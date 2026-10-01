FROM --platform=linux/amd64 mirror.gcr.io/library/python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea AS base

ARG TERRAFORM_VERSION=1.15.8
ARG AWS_PROVIDER_VERSION=6.51.0

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    AWS_ACCESS_KEY_ID=test \
    AWS_SECRET_ACCESS_KEY=test \
    AWS_DEFAULT_REGION=us-east-1 \
    AWS_ENDPOINT_URL=http://aws:4566 \
    TF_CLI_CONFIG_FILE=/etc/terraformrc.offline

RUN apt-get update && apt-get install -y --no-install-recommends \
      bash ca-certificates curl unzip jq git ripgrep procps \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir awscli boto3 requests jsonschema pyyaml pytest

# Terraform plus an offline provider mirror, so init does not depend on the
# registry at solve time.
RUN curl -fsSL --retry 6 --retry-all-errors --retry-delay 5 -o /tmp/terraform.zip \
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
      '}' >/etc/terraformrc.offline

# OpenTofu is offered as an alternative; the contracts accept either tool.
RUN curl -fsSL --retry 6 --retry-all-errors --retry-delay 5 -o /tmp/tofu.zip \
      "https://github.com/opentofu/opentofu/releases/download/v1.10.6/tofu_1.10.6_linux_amd64.zip" \
    && unzip -q /tmp/tofu.zip tofu -d /usr/local/bin \
    && rm /tmp/tofu.zip \
    && chmod 0755 /usr/local/bin/tofu \
    && for tool in bash curl jq terraform tofu aws python3; do command -v "$tool" >/dev/null; done

COPY workspace/ /workspace/

RUN groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash agent \
    && mkdir -p /workspace/config /workspace/submission /workspace/evidence \
    && chown -R agent:agent /workspace/submission /workspace/evidence \
    # Host file modes are not trusted: owner-only (0600/0700) modes in the
    # build context would leave the contracts unreadable to the agent user.
    && chmod a+rX /workspace \
    && chmod -R a+rX /workspace/contracts

WORKDIR /workspace
USER agent
