# Online Boutique (GoogleCloudPlatform/microservices-demo) — evaluation add-on

Upstream: <https://github.com/GoogleCloudPlatform/microservices-demo>
File: `release/kubernetes-manifests.yaml`, service version **v0.10.7**.

This directory exists for a **one-off honesty check**: run the Ops Agent against
an application we did *not* build, inject one fault, and see whether it can
diagnose it. It is **not** an eval scenario, and nothing here is wired into
`sandbox/faults.sh`, `sandbox/chaos.sh`, or `src/omagent/evals.py`.

## Files

| file | what |
| --- | --- |
| `kubernetes-manifests.upstream.yaml` | byte-for-byte upstream, vendored so the test is reproducible offline |
| `kubernetes-manifests.yaml` | the deployed form: upstream + the two kind adjustments below |
| `chaos-productcatalog-delay.yaml` | the NetworkChaos experiment used for the test |

## Deviations from upstream (complete list — two places, three lines)

**(1) `frontend-external` Service: `LoadBalancer` → `NodePort`.**

```diff
   name: frontend-external
 spec:
-  type: LoadBalancer
+  type: NodePort
   selector:
     app: frontend
   ports:
   - name: http
     port: 80
     targetPort: 8080
+    nodePort: 30080
```

Why: kind ships no cloud load-balancer controller, so a `LoadBalancer` Service
would sit `EXTERNAL-IP: <pending>` forever. `NodePort: 30080` instead lets us
`curl` the frontend to observe the injected symptom directly.

**(2) loadgenerator's init container: drop the pinned index digest.**

```diff
-        image: busybox:1.38.0@sha256:dc2d74b28e4cf8984fa52af1f39bc7c3d9c73760b41a74d629f5d11b1ab28616
+        image: busybox:1.38.0
```

Why: that `sha256:dc2d74b…` is the **multi-arch index** digest. The images are
side-loaded as single-platform (`linux/amd64`) images, whose manifest digest is
different, so containerd could not resolve the pinned reference offline and the
pod sat in `ImagePullBackOff`. Same tag, same bytes, no behaviour change.

**Why nothing else:** the upstream resource requests (~1.75 CPU / ~1.4 Gi total)
already fit the 5-node / 128-core kind cluster with room to spare, so there was
no reason to shrink them. Application behaviour, every other image, probes, env,
replica counts, and every other Service are untouched. In particular
`productcatalogservice` is left exactly as upstream ships it — the fault we
inject must land on an unmodified app or the test proves nothing.

## Result of the evaluation

See the task report. Summary: the agent diagnosed the **Chaos Mesh** fault
correctly (by reading Chaos Mesh's own namespace Events), but on an *identical,
silently injected* network delay it misdiagnosed the root cause as frontend CPU
throttling and proposed scaling frontend — which would not have fixed anything.

## Images

The kind nodes cannot reach the internet (`crictl pull` → connection reset), so
images are pulled on the host through the proxy and side-loaded:

```bash
export http_proxy=http://172.26.192.221:7890 https_proxy=http://172.26.192.221:7890
export PATH="$PWD/bin:$PATH"
docker pull <image> && kind load docker-image <image> --name om-sandbox
```

See `var/pull_boutique_images.sh` (scratch, not committed) for the 12-image loop.
