# AWS deployment

This project is deployed as a low-cost serverless app in `ap-southeast-1`.

Current stack:

- Stack: `symbol-counter`
- AWS profile used: `yarleePersonal`
- API URL: `https://2d3u6d1sfk.execute-api.ap-southeast-1.amazonaws.com`
- Website URL: `http://symbol-counter-279098292997-ap-southeast-1-web.s3-website-ap-southeast-1.amazonaws.com`
- ECR repository: `279098292997.dkr.ecr.ap-southeast-1.amazonaws.com/symbol-counter-lambda`
- Current image tag: `20260703-2`

## Update deployment

Build and push a new image tag:

```bash
docker build --platform linux/amd64 -f Dockerfile.lambda -t symbol-counter-lambda:latest .
docker tag symbol-counter-lambda:latest 279098292997.dkr.ecr.ap-southeast-1.amazonaws.com/symbol-counter-lambda:NEW_TAG
docker push 279098292997.dkr.ecr.ap-southeast-1.amazonaws.com/symbol-counter-lambda:NEW_TAG
```

Deploy the stack with the new image:

```bash
aws cloudformation deploy \
  --stack-name symbol-counter \
  --template-file aws/cloudformation.yaml \
  --parameter-overrides AppName=symbol-counter ImageUri=279098292997.dkr.ecr.ap-southeast-1.amazonaws.com/symbol-counter-lambda:NEW_TAG \
  --capabilities CAPABILITY_NAMED_IAM \
  --profile yarleePersonal \
  --region ap-southeast-1
```

Upload the frontend:

```bash
aws s3 sync static s3://symbol-counter-279098292997-ap-southeast-1-web \
  --delete \
  --exclude ".DS_Store" \
  --profile yarleePersonal \
  --region ap-southeast-1

printf 'window.API_BASE_URL = "https://2d3u6d1sfk.execute-api.ap-southeast-1.amazonaws.com";\n' > /tmp/symbol-counter-config.js

aws s3 cp /tmp/symbol-counter-config.js s3://symbol-counter-279098292997-ap-southeast-1-web/config.js \
  --content-type application/javascript \
  --profile yarleePersonal \
  --region ap-southeast-1
```

## Tear down

Empty the buckets first, then delete the stack:

```bash
aws s3 rm s3://symbol-counter-279098292997-ap-southeast-1-web --recursive --profile yarleePersonal --region ap-southeast-1
aws s3 rm s3://symbol-counter-279098292997-ap-southeast-1-files --recursive --profile yarleePersonal --region ap-southeast-1
aws cloudformation delete-stack --stack-name symbol-counter --profile yarleePersonal --region ap-southeast-1
```

Delete the ECR repository if you also want to remove stored container images:

```bash
aws ecr delete-repository --repository-name symbol-counter-lambda --force --profile yarleePersonal --region ap-southeast-1
```

## Notes

- The online layout analysis uses S3 presigned uploads and async Lambda processing.
- The legend upload still goes through API Gateway, so keep legend files under API Gateway payload limits.
- Teach-by-example remains local-only because the online processor is stateless by job.
- The website is public and has no login. Add auth before sharing it broadly.
