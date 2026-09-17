"""Skills from the AICP Skill Registry, pulled at startup.

For each handle in AICP_SKILLS: GET {AICP_SKILL_REGISTRY_URL}/skills/{handle}/resolve
(registry handles carry a 'skill:' prefix — we normalize) -> {version, url} where url is a
short-lived presigned link to the approved bundle (.tar.gz containing skill.yaml) -> download,
extract, and return the skill's instructions for the agent to fold into its system prompt.

Failure posture: warn+skip per skill (AICP_STRICT=1 to fail hard). The presigned URL is never
logged (it embeds a signature). Auth: AICP_SKILL_TOKEN bearer — interim until platform
token-vending; when that lands, only this file changes."""
from __future__ import annotations

import io
import os
import tarfile
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import httpx
import yaml

from aicp import config

log = config.log

# The agent's INBOUND vended token for THIS request (RFC 8693, aud=agent:<handle>), set by the
# entrypoint from the invoke JWT. This is the token-vending credential — the registry authorizes it
# as the agent's owning team, with NO standing per-agent secret. Falls back to the SA/static creds
# below when absent (e.g. a SigV4 invoke that carried no JWT), so existing agents keep working.
_request_token: ContextVar[str | None] = ContextVar("aicp_skill_request_token", default=None)


def set_request_token(token: str | None) -> None:
    """Called by the runtime shell per invocation with the inbound agent JWT (or None)."""
    _request_token.set(token or None)
_CACHE_DIR = Path("/tmp/aicp-skills")
_MAX_BUNDLE_BYTES = int(os.environ.get("AICP_SKILL_MAX_BYTES", str(50 * 1024 * 1024)))       # 50 MB download
_MAX_UNPACKED_BYTES = int(os.environ.get("AICP_SKILL_MAX_UNPACKED_BYTES", str(200 * 1024 * 1024)))  # 200 MB expanded


@dataclass
class Skill:
    handle: str          # bare handle, e.g. "invoice-helper"
    version: str
    name: str
    description: str
    instructions: str    # what you add to the system prompt
    path: Path           # extracted bundle dir (extra files the skill ships)
    apply: str = "on_demand"  # "always" -> folded into every prompt; "on_demand" -> model loads via use_skill


def _instructions_from_bundle(root: Path, manifest: dict) -> str:
    """The manifest may inline instructions or point at a file. The platform's skill.yaml
    convention is `entrypoint: prompt.md` (see docs/runbooks/skill.md); the other keys are
    accepted for tolerance, then SKILL.md/README.md as a last resort."""
    if isinstance(manifest.get("instructions"), str) and manifest["instructions"].strip():
        return manifest["instructions"]
    root_resolved = root.resolve()

    def _read_within(rel: str) -> str | None:
        # Confine manifest-declared paths to the bundle: reject absolute paths, `..`, and symlink
        # escapes (else a bundle could set entrypoint=/proc/self/environ and inject it).
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root_resolved)
        except ValueError:
            log.warning("skill manifest path '%s' escapes the bundle — ignored", rel)
            return None
        return candidate.read_text(encoding="utf-8", errors="replace") if candidate.is_file() else None

    for key in ("entrypoint", "instructions_file", "entry", "prompt"):
        rel = manifest.get(key)
        if isinstance(rel, str):
            text = _read_within(rel)
            if text is not None:
                return text
    for fallback in ("SKILL.md", "README.md"):
        if (root / fallback).is_file():
            return (root / fallback).read_text(encoding="utf-8", errors="replace")
    return ""


async def _bearer_token(client: httpx.AsyncClient) -> str:
    """Freshest available registry credential.

    Preferred (survives container restarts): mint a token per startup via Keycloak password
    grant — AICP_KC_TOKEN_URL + AICP_SKILL_CLIENT_ID(+SECRET) + AICP_SKILL_USERNAME/PASSWORD,
    all supplied via the AICP secret box. Fallback: a static AICP_SKILL_TOKEN (fine for a
    quick test; expires with Keycloak's access-token TTL). When platform token-vending lands,
    only this function changes."""
    # PREFERRED — token vending: the inbound vended agent token for this request. No standing
    # per-agent secret; the registry authorizes it as the agent's owning team. Present only when the
    # agent was invoked with a JWT (the production path).
    vended = _request_token.get()
    if vended:
        return vended
    token_url = os.environ.get("AICP_KC_TOKEN_URL", "")
    username = os.environ.get("AICP_SKILL_USERNAME", "")
    password = os.environ.get("AICP_SKILL_PASSWORD", "")
    client_id = os.environ.get("AICP_SKILL_CLIENT_ID", "")
    secret = os.environ.get("AICP_SKILL_CLIENT_SECRET", "")
    have_cc = bool(token_url and client_id and secret)
    # PREFERRED — the agent's OWN service-account (client_credentials, no human user). This is what AICP
    # injects for a deployed agent: the token's identity IS the agent (a team member), so the registry
    # authorizes can_use against the agent, not a borrowed human. When the platform-injected trio
    # (token_url + client_id + secret) is present it wins UNCONDITIONALLY — BYO username/password must
    # never silently demote a provisioned agent off its own identity onto a borrowed human's.
    if have_cc:
        try:
            r = await client.post(token_url, data={"grant_type": "client_credentials",
                                                   "client_id": client_id, "client_secret": secret})
            r.raise_for_status()
            return r.json()["access_token"]
        except Exception as e:
            log.warning("skill client_credentials mint failed (%s) — falling back", type(e).__name__)
    # Alternative — password grant as a configured user (interim / self-hosted). Only when the
    # client_credentials trio is NOT configured (else the block above already owns auth).
    if not have_cc and token_url and username and password and client_id:
        data = {"grant_type": "password", "client_id": client_id,
                "username": username, "password": password}
        if secret:
            data["client_secret"] = secret
        try:
            r = await client.post(token_url, data=data)
            r.raise_for_status()
            return r.json()["access_token"]
        except Exception as e:
            log.warning("skill password mint failed (%s) — falling back to AICP_SKILL_TOKEN",
                        type(e).__name__)
    return config.skill_token()


async def _load_one(client: httpx.AsyncClient, base: str, token: str, raw_handle: str) -> Skill | None:
    bare = raw_handle.removeprefix("skill:")
    registry_handle = f"skill:{bare}"  # the registry stores handles WITH the prefix
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = await client.get(f"{base}/skills/{quote(registry_handle, safe='')}/resolve", headers=headers)
    r.raise_for_status()
    meta = r.json()
    bundle = await client.get(meta["url"])  # presigned — no auth header
    bundle.raise_for_status()
    # Bound the download and the expansion so a large or zip-bomb bundle can't exhaust the agent's
    # memory/disk at startup (caps overridable via env for legitimately large skills).
    if len(bundle.content) > _MAX_BUNDLE_BYTES:
        raise ValueError(f"skill bundle exceeds {_MAX_BUNDLE_BYTES} bytes")
    dest = _CACHE_DIR / bare / str(meta.get("version", "current"))
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(bundle.content), mode="r:gz") as tf:
        total = 0
        for member in tf.getmembers():
            total += max(member.size, 0)
            if total > _MAX_UNPACKED_BYTES:
                raise ValueError(f"skill bundle expands past {_MAX_UNPACKED_BYTES} bytes")
        tf.extractall(dest, filter="data")
    manifest_path = next(dest.rglob("skill.yaml"), None)
    manifest = yaml.safe_load(manifest_path.read_text()) if manifest_path else {}
    manifest = manifest if isinstance(manifest, dict) else {}
    root = manifest_path.parent if manifest_path else dest
    apply = str(manifest.get("apply", "on_demand")).strip().lower()
    return Skill(
        handle=bare,
        version=str(meta.get("version", "")),
        name=str(manifest.get("name", bare)),
        description=str(manifest.get("description", "")),
        instructions=_instructions_from_bundle(root, manifest),
        path=root,
        apply=apply if apply in ("always", "on_demand") else "on_demand",
    )


async def aget_skills() -> list[Skill]:
    """Every loadable configured skill. [] when none configured (the normal day-1 state)."""
    handles = config.skill_handles()
    if not handles:
        return []
    base = config.skill_registry_url()
    if not base:
        msg = "AICP_SKILLS is set but AICP_SKILL_REGISTRY_URL is not"
        if config.strict():
            raise RuntimeError(msg)
        log.warning("%s — skills skipped", msg)
        return []
    skills: list[Skill] = []
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        token = await _bearer_token(client)
        if not token:
            log.warning("no skill-registry credential (AICP_SKILL_TOKEN or the AICP_SKILL_* "
                        "mint set) — resolve will likely 401")
        for handle in handles:
            try:
                skill = await _load_one(client, base, token, handle)
                if skill:
                    skills.append(skill)
                    log.info("skill '%s' v%s loaded", skill.handle, skill.version)
            except Exception as e:
                if config.strict():
                    raise
                # type-only logging: an HTTPStatusError's str() can embed the presigned URL
                log.warning("skill '%s' failed to load (%s) — skipped", handle, type(e).__name__)
    return skills


def render_skill_catalog(skills: list[Skill]) -> str:
    """System-prompt block for the loaded skills ('' when none) — PROGRESSIVE DISCLOSURE:
      * apply='always'    -> the skill's full guidance is folded in (applied to every response);
      * apply='on_demand' -> only name + description are advertised; the model loads the full
                             guidance by calling the `use_skill` tool when a task needs it.
    Keeps the per-request system prompt small: on-demand bodies live on disk (already downloaded),
    not in every prompt."""
    if not skills:
        return ""
    always = [s for s in skills if s.apply == "always"]
    on_demand = [s for s in skills if s.apply != "always"]
    parts: list[str] = []
    if always:
        parts.append("\n\n# Skills — always apply\nApply the following guidance to every response:")
        for s in always:
            header = f"\n\n## {s.name}" + (f" — {s.description}" if s.description else "")
            parts.append(header + ("\n" + s.instructions if s.instructions else ""))
    if on_demand:
        parts.append(
            "\n\n# Skills — available on demand\n"
            "These skills are available but NOT yet loaded. When the user's request calls for one, call "
            "the `use_skill` tool with its exact name to load its full guidance, then follow it. If none "
            "is relevant, just answer normally — do not mention skills."
        )
        for s in on_demand:
            parts.append(f"\n- **{s.name}**" + (f": {s.description}" if s.description else ""))
    return "".join(parts)


def make_skill_tool(skills: list[Skill]):
    """A LangChain tool the model calls to load a skill's full guidance by name (progressive
    disclosure). Returns None when there are no on-demand skills (nothing to load on request)."""
    on_demand = [s for s in skills if s.apply != "always"]
    if not on_demand:
        return None
    from langchain_core.tools import tool
    # Resolve by name OR handle, case-insensitively — the model tends to echo the displayed name.
    index = {s.handle.strip().lower(): s for s in skills}
    index.update({s.name.strip().lower(): s for s in skills})
    available = ", ".join(sorted(s.name for s in on_demand))

    @tool
    def use_skill(name: str) -> str:
        """Load the full step-by-step guidance for one of the AVAILABLE skills, by its exact name,
        then follow it to answer. Call this only when the user's request matches a skill listed under
        'Skills — available on demand'."""
        s = index.get((name or "").strip().lower())
        if not s:
            return f"No skill named '{name}'. Available skills: {available or 'none'}."
        return s.instructions or f"(skill '{s.name}' ships no guidance)"

    return use_skill


def render_skill_block(skills: list[Skill]) -> str:
    """DEPRECATED — kept so agent repos generated from an older template (whose graph.py imports this)
    keep building after the aicp shell is overlaid on redeploy. Folds every skill's full body into the
    prompt (the pre-progressive-disclosure behavior). New/updated graph.py should use
    render_skill_catalog + make_skill_tool instead."""
    if not skills:
        return ""
    parts = ["\n\n# Skills\nYou have the following skills. Apply them when relevant:"]
    for s in skills:
        header = f"\n\n## {s.name}" + (f" — {s.description}" if s.description else "")
        parts.append(header + ("\n" + s.instructions if s.instructions else ""))
    return "".join(parts)
