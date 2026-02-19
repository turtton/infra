# 内部で展開されており必要に応じてアクセスできるサービス

## Prometheus

```sh
kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090
```

## Longhorn

```sh
kubectl port-forward -n longhorn-system svc/longhorn-frontend 8080:80
```
