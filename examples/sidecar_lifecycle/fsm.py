"""Sidecar lifecycle FSM driven by community.docker.

Ensures a redis sidecar is pulled, running, and reachable. Demonstrates a
new angle for philip: managing **infrastructure** (containers) rather
than managing things **on** a host. All modules run on the controller
against the local Docker daemon; no ssh hosts involved.

Flow::

    inspect_image -> (missing?) pull_image -> inspect_container ->
        branch on container.State.Status:
            "running" -> health_check
            "exited"  -> start_container -> health_check
            absent    -> run_container   -> health_check
        health_check -> (ok) done
                     -> (fail) recreate_container -> health_check_again -> done | escalate

Modules:
  - community.docker.docker_image_info
  - community.docker.docker_image
  - community.docker.docker_container_info
  - community.docker.docker_container
  - community.docker.docker_container_exec

Run-of-show::

    uv run python examples/sidecar_lifecycle/fsm.py
    # Re-run: should hit running-already path
    docker stop philip-redis-demo
    uv run python examples/sidecar_lifecycle/fsm.py
    # ^ should detect "exited" -> start_container
    docker rm -f philip-redis-demo
    uv run python examples/sidecar_lifecycle/fsm.py
    # ^ absent -> run_container
"""

from __future__ import annotations

from typing import Any

from burr.core import Application, ApplicationBuilder, State, action, expr
from burr.tracking import LocalTrackingClient

from philip import initial_sentinels, module_action, snapshot_sentinels

IMAGE = "redis:7-alpine"
CONTAINER_NAME = "philip-redis-demo"
HEALTHCHECK_CMD = ["redis-cli", "ping"]


@module_action(
    "community.docker.docker_image_info",
    writes={"image_info": "images"},
)
def inspect_image(state: State) -> dict[str, Any]:
    return {"name": IMAGE}


@action(reads=["image_info"], writes=["image_present"])
def classify_image(state: State) -> State:
    return state.update(image_present=bool(state["image_info"]))


@module_action("community.docker.docker_image")
def pull_image(state: State) -> dict[str, Any]:
    return {"name": IMAGE, "source": "pull"}


@module_action(
    "community.docker.docker_container_info",
    writes={"container_info": "container"},
)
def inspect_container(state: State) -> dict[str, Any]:
    return {"name": CONTAINER_NAME}


@action(reads=["container_info"], writes=["container_status"])
def classify_container(state: State) -> State:
    info = state["container_info"] or {}
    status = (info.get("State") or {}).get("Status", "unknown") if info else "absent"
    return state.update(container_status=status)


@module_action("community.docker.docker_container")
def run_container(state: State) -> dict[str, Any]:
    return {
        "name": CONTAINER_NAME,
        "image": IMAGE,
        "state": "started",
        "detach": True,
        "restart_policy": "unless-stopped",
        "labels": {"philip": "sidecar-demo"},
    }


@module_action("community.docker.docker_container")
def start_container(state: State) -> dict[str, Any]:
    return {"name": CONTAINER_NAME, "state": "started"}


@module_action(
    "community.docker.docker_container_exec",
    writes={"health_stdout": "stdout", "health_rc": "rc"},
)
def health_check(state: State) -> dict[str, Any]:
    return {
        "container": CONTAINER_NAME,
        "argv": HEALTHCHECK_CMD,
        "chdir": "/",
    }


@module_action("community.docker.docker_container")
def recreate_container(state: State) -> dict[str, Any]:
    return {
        "name": CONTAINER_NAME,
        "image": IMAGE,
        "state": "started",
        "recreate": True,
        "detach": True,
        "restart_policy": "unless-stopped",
        "labels": {"philip": "sidecar-demo"},
    }


@action(reads=["container_status", "health_stdout", "health_rc"], writes=["outcome"])
def done(state: State) -> State:
    return state.update(
        outcome=(
            f"OK: container is {state['container_status']}; "
            f"healthcheck rc={state['health_rc']} stdout={(state['health_stdout'] or '').strip()!r}"
        )
    )


@action(reads=["container_status", "failure_reason"], writes=["outcome"])
def escalate(state: State) -> State:
    return state.update(
        outcome=(
            f"ESCALATE: container_status={state['container_status']} "
            f"reason={state['failure_reason']}"
        )
    )


snapshot_failure = snapshot_sentinels(write="failure_reason")


def build_application() -> Application:
    return (
        ApplicationBuilder()
        .with_actions(
            inspect_image=inspect_image,
            classify_image=classify_image,
            pull_image=pull_image,
            inspect_container=inspect_container,
            classify_container=classify_container,
            run_container=run_container,
            start_container=start_container,
            health_check=health_check,
            recreate_container=recreate_container,
            snapshot_failure=snapshot_failure,
            done=done,
            escalate=escalate,
        )
        .with_transitions(
            ("inspect_image", "classify_image"),
            ("classify_image", "inspect_container", expr("image_present")),
            ("classify_image", "pull_image"),
            ("pull_image", "snapshot_failure", expr("_last_failed")),
            ("pull_image", "inspect_container"),
            ("inspect_container", "classify_container"),
            ("classify_container", "health_check", expr("container_status == 'running'")),
            ("classify_container", "start_container", expr("container_status == 'exited'")),
            ("classify_container", "run_container"),
            ("run_container", "snapshot_failure", expr("_last_failed")),
            ("run_container", "health_check"),
            ("start_container", "snapshot_failure", expr("_last_failed")),
            ("start_container", "health_check"),
            ("health_check", "done", expr("health_rc == 0")),
            ("health_check", "recreate_container"),
            ("recreate_container", "snapshot_failure", expr("_last_failed")),
            ("recreate_container", "done"),
            ("snapshot_failure", "escalate"),
        )
        .with_tracker(LocalTrackingClient(project="philip-sidecar-lifecycle"))
        .with_state(
            **initial_sentinels(),
            image_info=[],
            image_present=False,
            container_info={},
            container_status="",
            health_stdout="",
            health_rc=-1,
            failure_reason="",
        )
        .with_entrypoint("inspect_image")
        .build()
    )


def main() -> None:
    app = build_application()
    last_action, _result, final_state = app.run(halt_after=["done", "escalate"])
    print(f"Final action:        {last_action}")
    print(f"Outcome:             {final_state['outcome']}")
    print(f"Container status:    {final_state['container_status']}")
    print(f"Image present:       {final_state['image_present']}")


if __name__ == "__main__":
    main()
