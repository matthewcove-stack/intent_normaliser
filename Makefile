.PHONY: dev down
# Edge contract targets
EDGE_REPO ?= ../../edge_proxy
EDGE_BASE_COMPOSE ?= docker-compose.yml

dev:
	bash $(EDGE_REPO)/scripts/dev_edge_up.sh
	docker compose -f $(EDGE_BASE_COMPOSE) -f compose.edge.yml up -d
	@echo "http://intent-normaliser.localhost"

down:
	docker compose -f $(EDGE_BASE_COMPOSE) -f compose.edge.yml down
