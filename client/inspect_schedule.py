"""Inspect chain state and final schedule for a schedule id."""

import json
import sys

import fabric_client as fc
import park_agent as pa
from config import CONTRACT_ADMM


def main():
    if len(sys.argv) != 2:
        raise SystemExit("usage: python3 inspect_schedule.py SCHEDULE_ID")
    schedule_id = sys.argv[1]
    state = pa.query_admm_state(schedule_id)
    final = fc.query_with_retry("Park1", CONTRACT_ADMM, "QueryFinalSchedule", [schedule_id])

    print("STATE_KEYS", sorted(state.keys()))
    slim_state = {k: v for k, v in state.items() if k not in ("lambdaScaled", "zScaled")}
    print(json.dumps(slim_state, indent=2, ensure_ascii=False))
    print("FINAL_KEYS", sorted(final.keys()))
    print(json.dumps(final, indent=2, ensure_ascii=False)[:6000])


if __name__ == "__main__":
    main()
