"""Hydrate os.environ from AWS SSM Parameter Store.

WHY THIS MUST BE IMPORTED BEFORE THE ENGINE MODULES

lead_engine.py, supabase_store.py, dataforseo.py, groq_ai.py and at_rest.py all
read os.environ AT IMPORT TIME. If this runs after them, the deployed runtime
starts with empty credentials and fails deep inside a run rather than at boot —
the worst kind of failure to debug. agentcore_app.py therefore calls
hydrate() before importing anything from the engine, and says so in a comment,
because the ordering is load-bearing and looks like a style accident.

WHY SSM AT ALL

The deployed zip must not contain credentials. AgentCore's CodeZip packager
copies codeLocation wholesale and does NOT read .gitignore, so .env had to move
outside python-app/ (see the .gitignore note). That leaves the runtime with no
config, and the alternatives are worse:

  - Inline in agentcore.json -> secret values committed to git, forever.
  - CDK addEnvironmentVariable from the shell -> values land in the
    CloudFormation template, readable by anyone with stack access.

An SSM path is not a secret, so agentcore.json can carry it safely. The values
live encrypted (SecureString/KMS) and only the runtime's execution role can read
them.

WHAT THIS DOES NOT DO

It does not keep secrets out of the process's memory or environment — once
hydrated they are ordinary env vars, as with any config. This protects them at
rest and in the template, which is the part that was actually leaking.

LOCAL DEVELOPMENT IS UNAFFECTED. With no LEADGEN_SECRETS_PATH set this is a
no-op, and the repo-root .env is used exactly as before.
"""

import os

# Set this on the deployed runtime only. Its absence is what makes local dev
# skip SSM entirely.
SECRETS_PATH_ENV = "LEADGEN_SECRETS_PATH"
DEFAULT_REGION = "us-west-2"

# Paths end like /leadgen/SUPABASE_SERVICE_KEY; the runtime wants the bare name.
def _leaf(name):
    return name.rsplit("/", 1)[-1].strip()


def hydrate(path=None, region=None, client=None, env=None):
    """Populate os.environ from SSM. Returns a small report (never the values).

    Raises if the path is configured but nothing was loaded — a runtime that
    boots with no credentials and discovers it mid-run is far worse than one
    that refuses to start.
    """
    env = env if env is not None else os.environ
    path = (path or env.get(SECRETS_PATH_ENV) or "").strip()

    if not path:
        return {"loaded": 0, "skipped": f"{SECRETS_PATH_ENV} not set"}

    if client is None:
        import boto3

        client = boto3.client(
            "ssm", region_name=region or env.get("AWS_REGION") or DEFAULT_REGION)

    names = []
    # get_parameters_by_path caps at 10 per call unless paginated.
    paginator = client.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(
            Path=path, WithDecryption=True, Recursive=True):
        for param in page.get("Parameters", []):
            name = _leaf(param.get("Name") or "")
            if not name:
                continue
            # setdefault: a real environment variable, or a locally-loaded
            # .env, wins over SSM. Only genuinely missing config is filled in.
            env.setdefault(name, param.get("Value") or "")
            names.append(name)

    if not names:
        raise RuntimeError(
            f"{SECRETS_PATH_ENV}={path!r} but SSM returned no parameters. The "
            "runtime would start with no credentials; refusing to continue. "
            "Check the parameter path and the execution role's "
            "ssm:GetParametersByPath permission.")

    return {"loaded": len(names), "path": path, "names": sorted(names)}
