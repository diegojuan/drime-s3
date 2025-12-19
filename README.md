# Drime S3 Gateway

S3-compatible interface for Drime Cloud storage.

## Features

- Full S3 API compatibility (GET, PUT, DELETE, HEAD, LIST)
- AWS Signature V4 chunked upload support
- Automatic retry with exponential backoff for API errors
- Local MD5 metadata caching for ETag consistency
- Case-insensitive folder matching


## Installation

```bash
pip install .
```

## Usage

- By default the bucket name is 'default'
- API key and secret can be a random string, I prefer to use drime/drime, just to know where I'm pointing


```bash
# Set your API key
export DRIME_API_KEY="your-api-key"

# Start the gateway
drime-s3

# Options
drime-s3 --host 0.0.0.0 --port 8081 --debug
```

## AWS CLI

```bash
# Configure profile (use any values for keys)
aws configure --profile drime

# List files
aws --profile drime --endpoint-url http://localhost:8081 s3 ls s3://default/

# Upload file
aws --profile drime --endpoint-url http://localhost:8081 s3 cp file.txt s3://default/

# Download file
aws --profile drime --endpoint-url http://localhost:8081 s3 cp s3://default/file.txt ./

# Delete file
aws --profile drime --endpoint-url http://localhost:8081 s3 rm s3://default/file.txt
```

## Docker

```bash
# Build
docker build -t drime-s3 .

# Run
docker run -e DRIME_API_KEY=your-key -p 8081:8081 drime-s3
```

## Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `DRIME_API_KEY` | Your Drime Cloud API key | Yes |

## Tested With

- AWS CLI v2
- Duplicati Backup
- S3 Browser
- Restic
- Rclone

## License

MIT
