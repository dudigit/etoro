# Helm

## Access The app from public

In order to access the app via Ingress you need to get get the external IP of the Ingress controller from the service address.
To get it you need to run:

```bash
kubectl get svc nginx-ingress-controller -n ingress -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
```

## Debug

To debug access to the application via the service run:

```bash
kubectl run busybox -n dudi --rm -it --image=busybox --restart=Never -- wget -qO- http://simple-web
```

# Trigger the job in Jenkis

```
http://20.16.209.109:8080/job/simple-web/
```

## Notes

The following annotation was added to the ingress definition:
```
nginx.ingress.kubernetes.io/rewrite-target: /
```
Reason:
When using a path (/dudi) the app likely doesn't know about.
If you run the following that will not work sice the appliction dose not handle the endpoint dudi:

```bash
kubectl run busybox -n dudi --rm -it --image=busybox --restart=Never -- wget -qO- http://simple-web/dudi
```