#!/usr/bin/env bash
# Run this ONCE on a fresh Ubuntu 22.04 VM (inside VMware) to install every
# tool the project needs: Docker, kubectl, minikube, Ansible, wrk2 build deps.
set -euo pipefail

echo ">> Updating apt"
sudo apt-get update -y
sudo apt-get install -y ca-certificates curl gnupg lsb-release git build-essential \
  libssl-dev libgit2-dev luajit luarocks software-properties-common jq

echo ">> Installing Docker Engine"
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(lsb_release -cs) stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update -y
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"

echo ">> Installing kubectl"
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
rm -f kubectl

echo ">> Installing minikube"
curl -LO https://storage.googleapis.com/minikube/releases/latest/minikube-linux-amd64
sudo install minikube-linux-amd64 /usr/local/bin/minikube
rm -f minikube-linux-amd64

echo ">> Installing Ansible"
sudo apt-get install -y python3-pip
python3 -m pip install --user ansible

echo ">> Building wrk2 (not packaged in apt)"
if [ ! -d "$HOME/wrk2" ]; then
  git clone https://github.com/giltene/wrk2.git "$HOME/wrk2"
fi
make -C "$HOME/wrk2"
sudo install "$HOME/wrk2/wrk" /usr/local/bin/wrk
# the binary is still called "wrk" — it IS wrk2 (constant throughput build)

echo ">> Versions installed:"
docker --version
kubectl version --client
minikube version
ansible --version | head -1
wrk --version || true

echo
echo "!! Log out and back in (or run 'newgrp docker') for the docker group to take effect."
