import kopf
import logging
import ast
import time
import json
import kubernetes.client
from datetime import datetime, timezone

_recently_remediated = {}
REMEDIATION_COOLDOWN_SECONDS = 60


def log_event(event_type, namespace, pod_name, **extra):
    """Emit a structured JSON log line for a crash-loop event."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event_type,
        "namespace": namespace,
        "pod": pod_name,
        **extra,
    }
    print(json.dumps(record))


@kopf.on.field('pods', field='status.phase')
def watch_pod_status(old, new, name, namespace, **kwargs):
    logging.info(f"Pod {namespace}/{name} phase changed: {old} -> {new}")


def clean_logs(logs):
    if isinstance(logs, bytes):
        return logs.decode('utf-8', errors='replace')
    if isinstance(logs, str) and logs.startswith("b'") and logs.endswith("'"):
        try:
            return ast.literal_eval(logs).decode('utf-8', errors='replace')
        except Exception:
            return logs
    return logs


def diagnose_crash(namespace, pod_name, container_name, container_status):
    last_state = container_status.get('lastState', {}).get('terminated', {})
    exit_code = last_state.get('exitCode')
    reason = last_state.get('reason')

    log_snippet = "N/A"
    try:
        v1 = kubernetes.client.CoreV1Api()
        logs = v1.read_namespaced_pod_log(
            name=pod_name,
            namespace=namespace,
            container=container_name,
            previous=True,
            tail_lines=10,
        )
        log_snippet = clean_logs(logs).strip()
    except Exception as e:
        log_snippet = f"(could not fetch logs: {e})"

    return {
        "exit_code": exit_code,
        "reason": reason,
        "log_snippet": log_snippet,
    }


def remediate(namespace, pod_name):
    try:
        v1 = kubernetes.client.CoreV1Api()
        v1.delete_namespaced_pod(name=pod_name, namespace=namespace)
        return True, "Deleted pod; controller will recreate it."
    except Exception as e:
        return False, f"Remediation failed: {e}"


def should_remediate(pod_key):
    last_time = _recently_remediated.get(pod_key)
    now = time.time()
    if last_time and (now - last_time) < REMEDIATION_COOLDOWN_SECONDS:
        return False
    _recently_remediated[pod_key] = now
    return True


@kopf.on.field('pods', field='status.containerStatuses')
def watch_container_status(old, new, name, namespace, **kwargs):
    if not new:
        return
    for container in new:
        waiting = container.get('state', {}).get('waiting', {})
        reason = waiting.get('reason')
        if reason == 'CrashLoopBackOff':
            restart_count = container.get('restartCount', 0)
            diagnosis = diagnose_crash(namespace, name, container['name'], container)

            log_event(
                "crash_loop_detected",
                namespace, name,
                container=container['name'],
                restarts=restart_count,
                exit_code=diagnosis['exit_code'],
                reason=diagnosis['reason'],
                logs=diagnosis['log_snippet'],
            )

            pod_key = f"{namespace}/{name}"
            if should_remediate(pod_key):
                success, message = remediate(namespace, name)
                log_event(
                    "remediation_attempted",
                    namespace, name,
                    success=success,
                    message=message,
                )
            else:
                log_event(
                    "remediation_skipped",
                    namespace, name,
                    reason="cooldown_active",
                )