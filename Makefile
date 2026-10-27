.DEFAULT_GOAL := help

SBX_HOST ?= 192.168.200.10
SBX_USER ?= sbx
SSH_KEY  ?= ~/.ssh/sbx_host

.PHONY: help ssh

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*## "} {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

ssh: ## Open a shell on the sandbox node
	ssh -i $(SSH_KEY) $(SBX_USER)@$(SBX_HOST)
