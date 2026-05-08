# Trigger the job in Jenkis

```
http://20.16.209.109:8080/job/simple-web/
```

# Jenkins pipeline

This document explains the **Deploy** stage in [`Jenkinsfile`](Jenkinsfile) — specifically the `helm upgrade --install` block and the following `kubectl rollout status` check.

## Snippet under discussion

```bash
helm upgrade --install "$RELEASE_NAME" "$CHART_PATH" \
  --namespace "$NAMESPACE" \
  --set image.tag="$IMAGE_TAG_PARAM" \
  --atomic \
  --wait \
  --timeout 10m

kubectl rollout status "deployment/$RELEASE_NAME" \
  -n "$NAMESPACE" \
  --timeout=300s
```

---

## Why `helm upgrade --install` instead of `helm install`

| Command | When you use it |
|--------|------------------|
| `helm install` | Only when you are sure **no release with that name** exists in the target namespace. A second run fails with “cannot re-use a name that is still in use”. |
| `helm upgrade` | Only when a release **already exists**. If none exists, `helm upgrade` alone fails. |
| `helm upgrade --install` | **Idempotent deploy**: if the release is missing it behaves like `install`; if it exists it behaves like `upgrade`. One pipeline path covers first deploy and every subsequent deploy. |

For CI/CD you almost always want **`upgrade --install`** so the same job can bootstrap a new environment and roll forward existing ones without branching logic.

---

## `helm upgrade --install` arguments (what each one does)

### `"$RELEASE_NAME"`

Helm **release name** — the logical name of this deployment in Helm’s release storage (e.g. `simple-web`). It must be stable across runs so upgrades target the same release.

### `"$CHART_PATH"`

Path to the chart directory in the checked-out repo (here `helm/simple-web`). Helm renders templates from that chart and applies the resulting manifests.

### `--namespace "$NAMESPACE"`

Install or upgrade the release **inside that Kubernetes namespace**. Without it, Helm uses the current kube-context default namespace, which is easy to get wrong in shared agents.

### `--set image.tag="$IMAGE_TAG_PARAM"`

Overrides `image.tag` at deploy time ( Jenkins parameter). This lets you promote a specific build tag without editing `values.yaml` in Git for every build. It merges on top of chart defaults.

### `--atomic`

If **anything** in the upgrade fails while Helm is waiting (with `--wait`), Helm **rolls the release back** to the last successful revision. You avoid leaving the cluster half-updated when readiness or hooks fail.

### `--wait`

Helm waits until resources it tracks reach a **ready** state (e.g. workloads available, Jobs finished per Helm’s rules) before the command exits successfully. If something never becomes ready, the command fails (and with `--atomic`, triggers rollback).

### `--timeout 10m`

Maximum time Helm will keep waiting for readiness. Prevents the pipeline hanging indefinitely on stuck images, failing PVCs, or broken probes. Ten minutes is a common upper bound; tune if your app is slow to pull or start.

---

## Why `kubectl rollout status` is used **in addition** to `helm upgrade ... --wait`

`helm --wait` and `kubectl rollout status` overlap partly but they are **not identical**:

# Use cases 

Specific Cases where this happens:

1. **The "Timeout Mismatch" (The most likely culprit)**

If your deployment takes 8 minutes to fully stabilize:
Helm succeeds because 8 minutes is less than its 10-minute (10m) limit.

Kubectl fails if the deployment is still finishing a rolling update (e.g., waiting for the old pods to terminate) and it exceeds the --timeout=300s. The command will exit with an error even if the new pods are technically healthy.

2. **The progressDeadlineSeconds Trap**

Kubernetes Deployments have a property in the YAML called progressDeadlineSeconds (default is 600s/10m).

If a deployment exceeds this deadline, Kubernetes marks it as "Failed" in the status metadata, even if the pods eventually start.

Helm might see the pods are finally Ready and return a success.

Kubectl rollout status specifically looks at the Progressing condition. If it sees Status=False and Reason=ProgressDeadlineExceeded, it will report a FAIL, even if the app is actually running.

3. **Post-Deployment Scaling or HPA Drift**

If you have a Horizontal Pod Autoscaler (HPA) or a post-install hook:

Helm finishes its work and sees 3/3 pods ready. Success.

Immediately after, a post-install job or an HPA triggers a scale-up to 10 pods because of a CPU spike during startup.

Kubectl rollout status starts. It sees that the deployment is now "Progressing" again (waiting for 7 more pods).

If those 7 pods take longer than 300s to pull, Kubectl FAILS, despite the initial 3 pods being perfectly fine.