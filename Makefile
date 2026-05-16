.PHONY: generate deploy validate diff netbox-up netbox-down clean

generate:
	python3 generate.py --all

diff:
	git diff output/

deploy:
	@echo "Deploy configs (manual trigger)"
	python3 deploy.py --device spine1

validate:
	python3 validate.py

compliance:
	python3 compliance.py

netbox-up:
	docker-compose -f netbox/docker-compose.yml up -d

netbox-down:
	docker-compose -f netbox/docker-compose.yml down

clean:
	rm -rf output/*.conf
	rm -rf clab-bgp-config-automation/
