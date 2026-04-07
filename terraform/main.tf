terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  # Uncomment this block after creating the S3 bucket manually.
  # Gives you shared, persistent Terraform state — essential if more than one
  # person manages infra or if you run Terraform from GitHub Actions.
  #
  # backend "s3" {
  #   bucket = "YOUR-TERRAFORM-STATE-BUCKET"
  #   key    = "trading212/terraform.tfstate"
  #   region = "eu-west-2"
  # }
}

provider "aws" {
  region  = var.aws_region
  profile = "trading212"
}

# ── Data sources ────────────────────────────────────────────────────────────

# Latest Amazon Linux 2023 AMI — automatically picks the right one for the region
data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023*-x86_64"]
  }

  filter {
    name   = "virtualization-type"
    values = ["hvm"]
  }
}

# ── Networking — uses the default VPC to keep things simple ─────────────────

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# ── Security group ───────────────────────────────────────────────────────────

resource "aws_security_group" "app" {
  name        = "trading212-app"
  description = "Allow HTTP, HTTPS and SSH for the trading212 app"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = var.allowed_ssh_cidrs
  }

  ingress {
    description = "HTTP (Caddy redirects to HTTPS)"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "trading212-app" }
}

# ── SSH key pair ─────────────────────────────────────────────────────────────

resource "aws_key_pair" "deploy" {
  key_name   = "trading212-deploy"
  public_key = var.ssh_public_key
}

# ── EC2 instance ─────────────────────────────────────────────────────────────

resource "aws_instance" "app" {
  ami                    = data.aws_ami.al2023.id
  instance_type          = var.instance_type
  key_name               = aws_key_pair.deploy.key_name
  subnet_id              = tolist(data.aws_subnets.default.ids)[0]
  vpc_security_group_ids = [aws_security_group.app.id]

  # 30 GB root volume — enough for Docker images, Postgres data and logs
  root_block_device {
    volume_type           = "gp3"
    volume_size           = 30
    delete_on_termination = true
    encrypted             = true
  }

  user_data = templatefile("${path.module}/user_data.sh", {
    app_repo_url = var.app_repo_url
  })

  # Prevent accidental termination — change to false if you want to destroy
  disable_api_termination = true

  tags = { Name = "trading212-app" }
}

# ── Elastic IP — static address that survives reboots ───────────────────────

resource "aws_eip" "app" {
  domain = "vpc"
  tags   = { Name = "trading212-app" }
}

resource "aws_eip_association" "app" {
  instance_id   = aws_instance.app.id
  allocation_id = aws_eip.app.id
}

# ── Route 53 A record ────────────────────────────────────────────────────────

resource "aws_route53_record" "app" {
  zone_id = var.route53_zone_id
  name    = var.domain_name
  type    = "A"
  ttl     = 300
  records = [aws_eip.app.public_ip]
}
