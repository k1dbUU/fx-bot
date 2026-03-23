"""
nucleus_job_agent.py — Migration shim
Part of the K-I-D-B-U-U → Nucleus Operator alias migration (Step 2).

This file exists so the workflow can call `python nucleus_job_agent.py`
while kidbuu_job_agent.py still contains all the real code.
Both filenames work during the transition — no crash risk.

Once kidbuu_job_agent.py is renamed to nucleus_job_agent.py (Step 5),
delete this file.
"""

import kidbuu_job_agent as _agent
import asyncio

if __name__ == "__main__":
    asyncio.run(_agent.run())
