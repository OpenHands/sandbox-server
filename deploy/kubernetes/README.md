# Running sandboxes on Kubernetes (agent-sandbox)

`RUNTIME=kubernetes` runs each sandbox as a pod managed by
[kubernetes-sigs/agent-sandbox](https://github.com/kubernetes-sigs/agent-sandbox)
instead of a local Docker container. Compared with the docker backend this gains
multi-node scheduling, pause/resume that keeps a persistent volume, and optional
gVisor / Kata isolation. Compared with the hosted remote runtime it is
self-hostable on any conformant cluster, from kind to GKE.

Each sandbox is a `SandboxClaim`; the controller turns that into a `Sandbox` and a
pod running the agent server. Nothing is stored in the app server: the cluster is
the source of truth, exactly as the docker backend treats the docker daemon.

## Prerequisites

1. A cluster with the agent-sandbox controller and extensions installed:

   ```bash
   export VERSION=v0.5.2
   kubectl apply -f "https://github.com/kubernetes-sigs/agent-sandbox/releases/download/${VERSION}/sandbox-with-extensions.yaml"
   kubectl -n agent-sandbox-system rollout status deploy --timeout=180s
   ```

2. The template, pool, and RBAC from this directory:

   ```bash
   kubectl apply -f sandboxtemplate.yaml
   kubectl apply -f sandboxwarmpool.yaml
   kubectl apply -f rbac.yaml        # only needed when the app server runs in-cluster
   ```

## Configuration

```bash
export RUNTIME=kubernetes           # or k8s
```

Everything else has a default; override through config as needed:

| Setting | Default | Purpose |
|---|---|---|
| `namespace` | `default` | Namespace holding the pool and sandboxes |
| `warm_pool` | unset | Pool to claim from. When unset the sandbox spec id is used, so several pools can be offered as runtimes |
| `sandbox_url_pattern` | `http://{sandbox_id}.{namespace}.svc.cluster.local:{port}` | How a sandbox is reached. Placeholders: `{sandbox_id}`, `{namespace}`, `{port}` |
| `webhook_base_url` | in-cluster service URL | This app server, as reachable *from* a sandbox pod |
| `max_num_sandboxes` | `10` | Concurrency cap; the oldest sandboxes are paused beyond it |
| `inject_session_key` | `true` | Give every sandbox its own session API key |
| `shutdown_after_seconds` | unset | TTL safety net; the controller deletes the claim afterwards |

Pod-level concerns — image, CPU/memory, `runtimeClassName`, network policy, volumes
— live in the `SandboxTemplate`, not here.

### Reaching sandboxes from a browser

The default `sandbox_url_pattern` is cluster-internal, which is right when the app
server runs in the cluster and proxies traffic. Agent Canvas talks to the agent
server directly, so a browser-facing deployment needs an externally routable
pattern, for example `https://{sandbox_id}.sandboxes.example.com`, backed by an
Ingress or Gateway that routes to the per-sandbox Service.

### Session keys and warm pools

The app server gives each sandbox a unique session API key by injecting it as an
environment variable on the claim. agent-sandbox cold starts a pod whenever a claim
injects environment variables, so pre-warmed replicas are only used when
`inject_session_key` is `false` — appropriate for a single-user deployment whose
template already carries a key, but not for shared clusters.

## Verifying

```bash
kubectl get sandboxclaim,sandbox,pods
```

A running conversation shows a claim, a Sandbox reporting `Ready`, and a pod. The
app server pauses a sandbox by setting `spec.operatingMode: Suspended` (the pod is
removed, the volume is kept) and resumes it by setting `Running`.
