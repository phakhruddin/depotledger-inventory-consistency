# Two targets: a thin Docker CLI image that builds the supplied application
# images and writes the per-run configuration, and the verifier itself.

FROM mirror.gcr.io/library/docker:27.5.1-cli@sha256:851f91d241214e7c6db86513b270d58776379aacc5eb9c4a87e5b47115e3065c AS runtime-setup
RUN apk add --no-cache coreutils
COPY application/ /application/
COPY runtime/runtime.sh /runtime.sh


FROM --platform=linux/amd64 mirror.gcr.io/library/python:3.12-slim AS verifier
ARG TERRAFORM_VERSION=1.15.8
ARG AWS_PROVIDER_VERSION=6.51.0
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/tests \
    TF_CLI_CONFIG_FILE=/etc/terraformrc.offline

RUN apt-get update && apt-get install -y --no-install-recommends bash curl jq unzip \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir awscli boto3 jsonschema pytest requests \
    && curl -fsSL --retry 6 --retry-all-errors --retry-delay 5 -o /tmp/terraform.zip \
         "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_amd64.zip" \
    && unzip -q /tmp/terraform.zip -d /usr/local/bin \
    && rm /tmp/terraform.zip \
    && mkdir -p /opt/terraform-provider-mirror /tmp/provider-seed \
    && printf '%s\n' \
      'terraform {' \
      '  required_providers {' \
      '    aws = {' \
      '      source = "hashicorp/aws"' \
      "      version = \"=${AWS_PROVIDER_VERSION}\"" \
      '    }' \
      '  }' \
      '}' >/tmp/provider-seed/main.tf \
    && TF_CLI_CONFIG_FILE=/dev/null terraform -chdir=/tmp/provider-seed init -backend=false -input=false \
    && TF_CLI_CONFIG_FILE=/dev/null terraform -chdir=/tmp/provider-seed providers mirror /opt/terraform-provider-mirror \
    && rm -rf /tmp/provider-seed \
    && printf '%s\n' \
      'disable_checkpoint = true' \
      'provider_installation {' \
      '  filesystem_mirror {' \
      '    path = "/opt/terraform-provider-mirror"' \
      '    include = ["registry.terraform.io/hashicorp/aws"]' \
      '  }' \
      '}' >/etc/terraformrc.offline \
    && curl -fsSL --retry 6 --retry-all-errors --retry-delay 5 -o /tmp/tofu.zip \
         "https://github.com/opentofu/opentofu/releases/download/v1.10.6/tofu_1.10.6_linux_amd64.zip" \
    && unzip -q /tmp/tofu.zip tofu -d /usr/local/bin \
    && rm /tmp/tofu.zip \
    && chmod 0755 /usr/local/bin/tofu \
    # The contracts promise the verifier has the same tools the agent had.
    # Fail the image build rather than a trial if that stops being true.
    && for tool in bash curl jq terraform tofu aws python3; do command -v "$tool" >/dev/null; done

COPY . /tests
COPY contracts/ /workspace/contracts/

RUN groupadd --gid 10001 verifier \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash verifier \
    && mkdir -p /logs/verifier /workspace/config /workspace/submission /workspace/evidence \
    && chown -R verifier:verifier /logs/verifier /workspace \
    # Host file modes are not trusted: a checkout with owner-only (0600/0700)
    # modes would otherwise leave /tests unreadable to the verifier user.
    && chmod -R a+rX /tests /workspace/contracts \
    && chmod 0555 /tests/test.sh

WORKDIR /tests
USER verifier
CMD ["sleep", "infinity"]
