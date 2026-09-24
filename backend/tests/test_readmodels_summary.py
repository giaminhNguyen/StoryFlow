"""WorkflowSummary.completed_projects (used by the frontend list progress)."""

from test_phase5_integration import Stack


def test_completed_projects_counts_only_finished_pipelines(tmp_path):
    stack = Stack(tmp_path)
    try:
        done = stack.new_workflow("done")
        idle = stack.new_workflow("idle")
        stack.workflows.start(done)
        stack.assign_discovered_runner(done)
        stack.drive(done, lambda s: s.display_state == "completed")
        by_id = {w.id: w for w in stack.read.list_workflows()}
        assert by_id[done].completed_projects == 1 and by_id[done].project_count == 1
        assert by_id[idle].completed_projects == 0 and by_id[idle].project_count == 1
    finally:
        stack.app.close()
