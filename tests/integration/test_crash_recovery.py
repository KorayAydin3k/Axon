import os
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.engine.workflow_engine import WorkflowRunConflict, execute_workflow
from app.persistence.models import FollowupTask, Workflow, WorkflowEvent, WorkflowStep
from app.tools.registry import SUPPORT_TOOLS


CRASH_EXIT_CODE = 73

CRASH_WORKER = """
import os
import sys
from uuid import UUID

from sqlalchemy.orm import Session

from app.engine.workflow_engine import SupportPlan, execute_workflow
from app.persistence.database import engine
from app.tools.registry import SUPPORT_TOOLS

create_tasks = SUPPORT_TOOLS["create_followup_tasks"]


def crash_after_write(session, workflow_id, inputs, arguments):
    create_tasks(session, workflow_id, inputs, arguments)
    session.flush()
    os._exit(int(sys.argv[2]))


SUPPORT_TOOLS["create_followup_tasks"] = crash_after_write

with Session(engine) as session:
    execute_workflow(session, UUID(sys.argv[1]), SupportPlan.SUPPORT_FOLLOWUP)
"""


@unittest.skipUnless(os.getenv("DATABASE_URL"), "Requires a PostgreSQL DATABASE_URL")
class CrashRecoveryTest(unittest.TestCase):
    def test_resume_after_worker_exits_during_task_creation(self):
        from app.persistence.database import engine

        with Session(engine) as session, session.begin():
            workflow = Workflow(request="Crash recovery integration test")
            session.add(workflow)
            session.flush()
            workflow_id = workflow.id

        def clean_up():
            with Session(engine) as session, session.begin():
                for model in (FollowupTask, WorkflowStep, WorkflowEvent):
                    session.execute(delete(model).where(model.workflow_id == workflow_id))
                session.execute(delete(Workflow).where(Workflow.id == workflow_id))

        self.addCleanup(clean_up)

        worker = subprocess.run(
            [sys.executable, "-c", CRASH_WORKER, str(workflow_id), str(CRASH_EXIT_CODE)],
            timeout=30,
            check=False,
        )
        self.assertEqual(worker.returncode, CRASH_EXIT_CODE)

        step_query = (
            select(WorkflowStep)
            .where(WorkflowStep.workflow_id == workflow_id)
            .order_by(WorkflowStep.position)
        )
        task_query = select(FollowupTask).where(FollowupTask.workflow_id == workflow_id)

        with Session(engine) as session:
            self.assertEqual(session.get(Workflow, workflow_id).status, "RUNNING")
            self.assertEqual(
                [step.name for step in session.scalars(step_query)],
                ["find_tickets", "summarize_tickets"],
            )
            self.assertEqual(session.scalars(task_query).all(), [])

        completed_tools = {
            name: Mock(side_effect=AssertionError(f"Completed tool reran: {name}"))
            for name in ("list_tickets", "summarize_tickets")
        }
        with patch.dict(SUPPORT_TOOLS, completed_tools), Session(engine) as session:
            result = execute_workflow(session, workflow_id)
            self.assertEqual(result.status, "COMPLETED")

        for tool in completed_tools.values():
            tool.assert_not_called()

        with Session(engine) as session:
            self.assertEqual(
                [step.name for step in session.scalars(step_query)],
                ["find_tickets", "summarize_tickets", "create_tasks"],
            )
            tasks = session.scalars(task_query).all()
            self.assertEqual(len(tasks), 3)
            self.assertEqual(
                {task.ticket_id for task in tasks}, {"T-102", "T-104", "T-101"}
            )

        with Session(engine) as session:
            with self.assertRaises(WorkflowRunConflict):
                execute_workflow(session, workflow_id)
