TAG1 = weisburd/majiq:v1
TAG2 = weisburd/majiq:latest

all: build push

build:
	docker build -t $(TAG1) -t $(TAG2) .

push:
	docker push $(TAG1)
	docker push $(TAG2)
