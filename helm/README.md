# Helm

## Access

In order to get the external IP to access you need to run:

```bash
kubectl get svc nginx-ingress-controller -n ingress -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
```

## Debug

To debug access to the service you can run:

```bash
kubectl run busybox -n dudi --rm -it --image=busybox --restart=Never -- wget -qO- http://simple-web
```

## Notes

The following annotation was added to the ingress definition:
```
nginx.ingress.kubernetes.io/rewrite-target: /
```
Reason:
When using a path (/dudi) the app likely doesn't know about.