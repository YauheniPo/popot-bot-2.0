#!/usr/bin/env bash
# Coverage + PR-scoped Sonar scan for popot-bot-2.0
set -euo pipefail
cd "$(dirname "$0")" || exit 1

# Python coverage
cd hermes
/tmp/v312/bin/python -m coverage run --source=. -m pytest ../hermes/runtime/test_workspace_launch.py ../hermes/ansible/test_workspace_ui.py -q
/tmp/v312/bin/python -m coverage xml -o ../hermes-coverage.xml

# JS coverage (runtime tests)
cd ../hermes/runtime
node --test test-workspace-*.mjs --experimental-test-coverage 2>&1 | tail -5

# SonarCloud PR-scoped scan
cd ..
cat > sonar-project.properties <<'EOF'
sonar.projectKey=YauheniPo_popot-bot-2.0
sonar.organization=yauhenipo
sonar.host.url=https://sonarcloud.io
sonar.login=${SONAR_TOKEN}
sonar.pullrequest.key=43
sonar.pullrequest.branch=feat/hermes-workspace-and-deploy-improvements
sonar.pullrequest.base=main
sonar.sources=.
sonar.inclusions=**/*.py,**/*.mjs,**/*.js,**/*.yml,**/*.yaml
sonar.exclusions=**/node_modules/**,**/__pycache__/**,**/.pytest_cache/**,**/test-*.mjs,**/*_test.py,**/test_*.py
sonar.python.coverage.reportPaths=hermes-coverage.xml
sonar.tests=hermes/runtime
sonar.test.inclusions=**/test-*.mjs
EOF

sonar-scanner -Dsonar.pullrequest.key=43 -Dsonar.pullrequest.branch=feat/hermes-workspace-and-deploy-improvements -Dsonar.pullrequest.base=main 2>&1 | tee sonar-scan.log
EOF
chmod +x /home/hermes/workspace/worktrees/pr43-sonar/.tmp-scan-full.sh