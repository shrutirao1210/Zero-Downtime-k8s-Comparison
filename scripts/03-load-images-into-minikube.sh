#!/usr/bin/env bash
# Pulls the images straight from Docker Hub into minikube's internal Docker
# daemon, so pods start instantly without hitting Docker Hub rate limits on
# every deploy. Run AFTER minikube is started (ansible playbook 01) and
# AFTER 02-build-and-push-images.sh has pushed the images.
set -euo pipefail
DOCKERHUB_USER="shrutimrao"
SERVICES=(api-gateway catalog-service price-service inventory-service shipping-service)
TAGS=(v1 v2)

eval "$(minikube docker-env)"
for svc in "${SERVICES[@]}"; do
  for tag in "${TAGS[@]}"; do
    echo ">> Pulling ${DOCKERHUB_USER}/${svc}:${tag} into minikube's Docker"
    docker pull "${DOCKERHUB_USER}/${svc}:${tag}"
  done
done
echo ">> Done. Images are now cached inside the minikube VM."
