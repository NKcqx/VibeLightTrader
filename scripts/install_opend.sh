#!/usr/bin/env bash
# Bootstrap script for installing Futu OpenD locally.
# OpenD is required for all OpenAPI / Anomaly Skill features (real-time quotes,
# K-line, paper trading, technical/capital anomaly detection).
set -euo pipefail

cat <<'EOF'
==> Step 1: Invoke Futu skill to install OpenD
    In your Cursor / Claude Code conversation, type:

        /install-futu-opend

    If the skill is not yet registered, install the skill bundle first:

        curl -L https://openapi.futunn.com/skills/opend-skills.zip -o /tmp/opend-skills.zip
        mkdir -p ~/.claude/skills && unzip -o /tmp/opend-skills.zip -d ~/.claude/skills/
        rm /tmp/opend-skills.zip

    Then re-run /install-futu-opend.

==> Step 2: Launch OpenD
    Open the OpenD GUI (downloaded in Step 1).
    Log in with your Futu (futubull / 富途牛牛) account.
    Confirm the status panel shows: listening on 127.0.0.1:11111.

==> Step 3: Verify connectivity
    From the repo root, with the project's conda env activated
    (the env you created in Quick Start step 1):

        conda activate vibe-trader
        python scripts/check_opend.py

    Expected output: "OK: OpenD reachable" plus an AAPL snapshot row.
EOF
