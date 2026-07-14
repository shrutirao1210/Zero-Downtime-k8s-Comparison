#!/usr/bin/env bash
# Builds all 5 microservice images and pushes them to Docker Hub under
# shrutimrao, tagged twice: once as v1 (the BLUE baseline) and once as v2
# (the new GREEN release). Run this BEFORE any Ansible playbook.
set -euo pipefail

DOCKERHUB_USER="shrutimrao"
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICES=(api-gateway catalog-service price-service inventory-service shipping-service)

echo ">> Checking Docker Hub auth"
DOCKER_CFG="${HOME}/.docker/config.json"
if [ -f "${DOCKER_CFG}" ] && grep -q '"https://index.docker.io/v1/"' "${DOCKER_CFG}"; then
  echo ">> Existing Docker Hub credentials found in ${DOCKER_CFG} — skipping login."
else
  echo ">> No cached credentials found. Running 'docker login'."
  docker login
fi

for svc in "${SERVICES[@]}"; do
  echo ">> Building ${svc} -> ${DOCKERHUB_USER}/${svc}:v1"
  docker build -t "${DOCKERHUB_USER}/${svc}:v1" "${PROJECT_ROOT}/microservices/${svc}"

  echo ">> Tagging ${svc} as v2 (the 'new version' for the Green deployment)"
  docker tag "${DOCKERHUB_USER}/${svc}:v1" "${DOCKERHUB_USER}/${svc}:v2"

  echo ">> Pushing ${svc}:v1"
  docker push "${DOCKERHUB_USER}/${svc}:v1"
  echo ">> Pushing ${svc}:v2"
  docker push "${DOCKERHUB_USER}/${svc}:v2"
done

echo ">> All images pushed to https://hub.docker.com/u/${DOCKERHUB_USER}"
