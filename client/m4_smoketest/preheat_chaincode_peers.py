"""
Preheat all park peers for the currently committed energy chaincode.

Why this exists:
  Fabric dev networks build chaincode Docker images lazily. After an upgrade, a
  peer may have installed the package but still fail the first real transaction
  with "No such image: dev-peer0.<org>...". That is an infrastructure failure,
  not a mask/KKT failure. This script makes that state explicit before running
  M4 smoke tests or full ADMM tests.

The script is intentionally isolated: it only performs read-only queries and,
if Docker already has an equivalent image for the same package ID on another
peer, adds the missing local tag for the failing peer.
"""

import os
import re
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import fabric_client as fc
from config import CONTRACT_AUDIT, PARK_IDS


NO_IMAGE_RE = re.compile(r"No such image:\s+([^\s]+:latest)")
PACKAGE_TOKEN_RE = re.compile(r"(energy_[^-]+-[0-9a-f]{64})-[0-9a-f]{64}:latest")


def docker(args):
    result = subprocess.run(
        ["docker", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout


def parse_missing_image(error_text):
    match = NO_IMAGE_RE.search(error_text)
    return match.group(1) if match else None


def find_equivalent_image(missing_image):
    token_match = PACKAGE_TOKEN_RE.search(missing_image)
    if not token_match:
        return None
    package_token = token_match.group(1)
    lines = docker(["images", "--format", "{{.Repository}}:{{.Tag}}"]).splitlines()
    for image in lines:
        if image != missing_image and package_token in image:
            return image
    return None


def repair_missing_image(missing_image):
    source = find_equivalent_image(missing_image)
    if not source:
        return False, f"no equivalent image found for {missing_image}"
    docker(["tag", source, missing_image])
    return True, f"tagged {source} -> {missing_image}"


def query_peer(org):
    return fc.query(
        caller_org=org,
        contract=CONTRACT_AUDIT,
        function="QueryCreditScore",
        args=[org],
    )


def preheat_org(org, attempts=3):
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            query_peer(org)
            return True, f"{org} ready"
        except Exception as exc:
            last_error = str(exc)
            missing = parse_missing_image(last_error)
            if missing:
                repaired, message = repair_missing_image(missing)
                if repaired:
                    print(f"    {org}: repaired missing image: {message}")
                    time.sleep(1.0)
                    continue
                return False, f"{org}: {message}"
            if attempt < attempts:
                time.sleep(2.0)
    return False, f"{org}: {last_error}"


def main():
    print("=" * 72)
    print("Preheat energy chaincode containers on Park peers")
    print("=" * 72)
    ok = True
    for org in PARK_IDS:
        ready, message = preheat_org(org)
        print(("  OK  " if ready else "  FAIL ") + message)
        ok = ok and ready
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
