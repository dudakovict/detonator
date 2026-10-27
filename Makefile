.DEFAULT_GOAL := help

SBX_HOST ?= 192.168.200.10
SBX_USER ?= sbx
SSH_KEY  ?= ~/.ssh/sbx_host
# Where the repo is synced to on the sandbox node, relative to the user's home.
NODE_DIR ?= detonator

SSH := ssh -i $(SSH_KEY) $(SBX_USER)@$(SBX_HOST)
TF  := terraform -chdir=infra/terraform
# Extra flags for terraform apply/destroy, e.g. TF_ARGS=-auto-approve
TF_ARGS ?=
# The golden image the VMs are built from: whatever sbx-linux-latest points to.
BASE_IMAGE = $$($(SSH) readlink /var/lib/libvirt/images/base/sbx-linux-latest.qcow2)
# Written by `make node`; used by kubectl and helm.
export KUBECONFIG ?= $(HOME)/.kube/detonator.yaml

.PHONY: help ssh node sync image image-test vms vms-destroy snapshot net-test deploy api-test worker e2e observability runner

help: ## List targets
	@grep -E '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*## "} {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

ssh: ## Open a shell on the sandbox node
	$(SSH)

node: ## Configure the sandbox node (KVM, libvirt, Packer) with Ansible
	cd infra/ansible && ansible-playbook node.yml

sync: ## Copy the repo to the sandbox node
	rsync -az --delete --exclude .git/ --exclude packer_cache/ --exclude output/ \
	  --exclude .terraform/ --exclude '*.tfstate*' \
	  -e "ssh -i $(SSH_KEY)" ./ $(SBX_USER)@$(SBX_HOST):$(NODE_DIR)/

image: sync ## Build the guest image with Packer on the sandbox node
	$(SSH) $(NODE_DIR)/infra/packer/linux-guest/build.sh

image-test: sync ## Boot the latest guest image once and exercise the agent
	$(SSH) $(NODE_DIR)/infra/packer/linux-guest/smoke-test.sh

vms: ## Create the isolated network and sandbox VMs from the latest image (Terraform)
	$(TF) init -input=false
	$(TF) apply $(TF_ARGS) -var "base_image=$(BASE_IMAGE)"

vms-destroy: ## Remove the sandbox VMs and network
	$(SSH) 'for vm in $$(virsh -c qemu:///system list --all --name | grep ^sbx-linux-); do \
	  virsh -c qemu:///system snapshot-delete $$vm clean 2>/dev/null; done; true'
	$(TF) destroy $(TF_ARGS) -var "base_image=$(BASE_IMAGE)"

snapshot: sync ## Take the "clean" snapshot of every sandbox VM
	$(SSH) $(NODE_DIR)/scripts/snapshot.sh

net-test: sync ## Prove from inside a VM that the network is isolated and sinkholed
	$(SSH) $(NODE_DIR)/scripts/net-test.sh

# The image is tagged with its own content ID (provenance off, so identical
# inputs give an identical ID): a changed image always gets a
# new tag and Kubernetes rolls the Deployment; an unchanged one is a no-op.
deploy: ## Build the API image, load it into k3s and install/upgrade the Helm release
	docker build -q --provenance=false -t detonator-api:build services/api >/dev/null
	tag=$$(docker image inspect -f '{{.Id}}' detonator-api:build | cut -c8-19) && \
	docker tag detonator-api:build detonator-api:$$tag && \
	docker save detonator-api:$$tag | $(SSH) sudo /usr/local/bin/k3s ctr -n k8s.io images import - >/dev/null && \
	helm upgrade --install detonator deploy/helm/detonator --namespace detonator \
	  --create-namespace --wait --timeout 5m --set api.image.tag=$$tag

api-test: ## Submit a harmless sample to the API and check it was stored and queued
	scripts/api-test.sh http://$(SBX_HOST):30080

worker: ## Install the detonation workers on the node, one per sandbox VM (Ansible)
	cd infra/ansible && ansible-playbook worker.yml

e2e: ## Submit the benign samples and check their verdicts and IOCs end to end
	scripts/e2e.sh http://$(SBX_HOST):30080

observability: ## Install Prometheus, Alertmanager and Grafana on k3s (Helm)
	helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null
	helm repo add grafana https://grafana.github.io/helm-charts >/dev/null
	helm dependency build deploy/helm/observability >/dev/null
	helm upgrade --install observability deploy/helm/observability --namespace monitoring \
	  --create-namespace --wait --timeout 10m

runner: ## Install the self-hosted GitHub Actions runner on the node (Ansible)
	cd infra/ansible && ansible-playbook runner.yml -e github_runner_token=$$(gh api -X POST \
	  repos/dudakovict/detonator/actions/runners/registration-token -q .token)
