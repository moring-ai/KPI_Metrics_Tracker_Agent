# The KPI tracker as a Lambda container image.
#
# AWS's base image already contains the Lambda runtime interface, so there is
# no bootstrap to write and nothing to get wrong about the entry contract:
# CMD is just "<module>.<function>".
#
# arm64 because it is cheaper than x86 on Lambda and this builds natively on an
# Apple Silicon Mac. To build on an Intel machine add --platform linux/arm64.
#
# The big win over the zip: pip runs INSIDE the target image, so the compiled
# wheels are correct by construction. The zip has to force
# --platform manylinux2014_aarch64 and can silently pick up macOS binaries.
FROM public.ecr.aws/lambda/python:3.12-arm64

# Dependencies first, in their own layer, so editing the code does not reinstall
# them on every build.
COPY deploy/lambda/requirements-lambda.txt .
RUN pip install --no-cache-dir -r requirements-lambda.txt -t "${LAMBDA_TASK_ROOT}"

# The application.
COPY lambda_handler.py "${LAMBDA_TASK_ROOT}/"
COPY kpi_tracker "${LAMBDA_TASK_ROOT}/kpi_tracker"
COPY kpi_mcp "${LAMBDA_TASK_ROOT}/kpi_mcp"
COPY data/members.json "${LAMBDA_TASK_ROOT}/data/members.json"

# The LAMBDA config, not the EC2 one: this filesystem is read-only and does not
# survive an invocation, so the history path must be an S3 URI.
COPY deploy/lambda/config.lambda.json "${LAMBDA_TASK_ROOT}/config.json"

CMD ["lambda_handler.handler"]
