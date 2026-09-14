#!/usr/bin/env bash
# Build and deploy the dashboard image, then roll ECS onto it.
#
# Run from the repo root:   bash deploy-dashboard.sh
#
# WHY A SCRIPT, AND WHY THIS ONE
#
# Deploying this app is four steps in a fixed order, and the failure mode of
# getting the order wrong is silent — an old image kept running while a build
# sits unused, which looks exactly like "my change didn't work". The steps:
#
#   1. zip the tree            (CodeBuild builds from S3, not from git)
#   2. upload + start build    (CodeBuild has docker; this machine does not)
#   3. wait for SUCCEEDED      (start-build returns immediately)
#   4. force a new deployment  (ECS will not pull :latest on its own)
#
# Step 4 is the one that gets forgotten. ECS caches the image by tag, so
# redeploying without --force-new-deployment leaves the old container running
# and the new image unreferenced.
#
# Requires: the AWS CLI, already authenticated to this account. No docker.

set -euo pipefail

REGION=us-west-2
ECR_REPO_URI=786896018790.dkr.ecr.us-west-2.amazonaws.com/leadgen-dashboard
BUCKET=leadgen-buildsrc-786896018790-us-west-2
PROJECT=leadgen-dashboard-build
CLUSTER=leadgen
SERVICE=leadgen-dashboard

cd "$(dirname "$0")"

echo "==> 1/4 packaging the source"
# An explicit include-list, not "zip everything": the repo-root .env must never
# reach S3, and venv/, .git/ and node_modules/ would add hundreds of MB to an
# upload whose useful content is a few hundred KB.
python - <<'PY'
import os, zipfile
SKIP_DIRS = {'__pycache__', '.pytest_cache', 'tests', 'outputs', 'node_modules', '.mypy_cache'}
# location_memo.json is a machine-generated lookup cache (grows without
# bound, diverges across ECS tasks on ephemeral disk). Shipping it would bake
# one machine's history into every image; a fresh container rebuilds it from
# locations.json, which DOES ship (its absence would trigger a bulk API
# re-download instead). err.log is a local accident, never an artefact.
SKIP_FILES = {'.env', '.last_run_id', 'location_memo.json', 'err.log'}
with zipfile.ZipFile('buildsrc.zip', 'w', zipfile.ZIP_DEFLATED) as z:
    z.write('buildspec.yml')
    z.write('Dockerfile.web')
    z.write('.dockerignore')
    for root, dirs, files in os.walk('python-app'):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if f in SKIP_FILES or f.endswith(('.pyc', '.pyo', '.log')):
                continue
            p = os.path.join(root, f).replace(os.sep, '/')
            z.write(p, p)
print('    %d KB' % (os.path.getsize('buildsrc.zip') // 1024))
PY

echo "==> 2/4 uploading and starting the build"
aws s3 cp buildsrc.zip "s3://$BUCKET/buildsrc.zip" --region "$REGION" --only-show-errors
BUILD_ID=$(aws codebuild start-build --project-name "$PROJECT" --region "$REGION" \
             --query 'build.id' --output text)
echo "    $BUILD_ID"

echo "==> 3/4 building (this takes a few minutes)"
while true; do
  sleep 20
  STATUS=$(aws codebuild batch-get-builds --ids "$BUILD_ID" --region "$REGION" \
             --query 'builds[0].buildStatus' --output text)
  case "$STATUS" in
    SUCCEEDED) echo "    build succeeded"; break ;;
    IN_PROGRESS) echo "    ...$STATUS" ;;
    *) echo "    BUILD $STATUS — see the CodeBuild log:" >&2
       echo "    https://$REGION.console.aws.amazon.com/codesuite/codebuild/projects/$PROJECT/history" >&2
       exit 1 ;;
  esac
done

echo "==> 4/4 rolling ECS"
# WARNING: this kills in-flight runs. Dashboard runs live on daemon threads in
# the worker, so a new deployment ends them mid-turn and their rows stay
# `running` forever. Before rolling, check POST /runs/list for live runs and
# wait or warn. (The durable fix is externalized runs — SQS + worker fleet —
# tracked as Phase 2.7, not this script.)
aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE" \
  --force-new-deployment --region "$REGION" --query 'service.serviceName' --output text

echo
echo "Live at: http://leadgen-alb-304776843.us-west-2.elb.amazonaws.com"
echo "Watch the roll:  aws ecs describe-services --cluster $CLUSTER --services $SERVICE \\"
echo "                   --region $REGION --query 'services[0].[runningCount,desiredCount]'"
