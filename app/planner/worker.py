import logging
from os import getenv
from time import sleep

from prometheus_client import start_http_server

from app.observability.telemetry import log_event, metrics_registry
from app.planner.jobs import claim_plan_job, run_plan_job


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if metrics_port := getenv("AXON_PLANNING_METRICS_PORT"):
        start_http_server(int(metrics_port), addr="0.0.0.0", registry=metrics_registry)

    while True:
        claim = None
        try:
            claim = claim_plan_job()
            if claim is not None:
                run_plan_job(claim)
                continue
        except Exception as error:
            fields: dict[str, object] = {"error_type": type(error).__name__}
            if claim is not None:
                fields.update(
                    workflow_id=str(claim.workflow_id), job_id=claim.job_id, request_id=claim.request_id
                )
            log_event("planning.worker_error", **fields)
        sleep(1)


if __name__ == "__main__":
    main()
