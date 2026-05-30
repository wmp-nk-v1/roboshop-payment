.PHONY: build run docker-build argocd-deploy clean

ECR_REPO = 293222827824.dkr.ecr.us-east-1.amazonaws.com/roboshop-payment

build:
	pip install -r requirements.txt

run:
	AMQP_HOST=localhost CART_URL=http://localhost:8003 USER_URL=http://localhost:8001 uvicorn main:app --host 0.0.0.0 --port 8005 --reload

docker-build:
	aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 293222827824.dkr.ecr.us-east-1.amazonaws.com
	docker build -t $(ECR_REPO):$(image_tag) .
	trivy image $(ECR_REPO):$(image_tag) -s CRITICAL,HIGH --ignore-unfixed
	docker push $(ECR_REPO):$(image_tag)

argocd-deploy:
	argocd login $(argocd_server) --skip-test-tls --username admin --password $(argocd_admin_password)
	argocd app create roboshop-payment --sync-policy auto --upsert \
		--repo https://github.com/nikkaushal/roboshop-helm-v1.git \
		--path . \
		--dest-server https://kubernetes.default.svc \
		--dest-namespace roboshop \
		--sync-option CreateNamespace=true \
		--helm-set image_tag=$(image_tag) \
		--values values/roboshop-payment.yml

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
