variable "aws_region" {
  description = "AWS region to deploy into"
  type        = string
  default     = "eu-west-2"  # London
}

variable "instance_type" {
  description = "EC2 instance type"
  type        = string
  default     = "t3.small"  # 2 vCPU, 2 GB RAM — plenty for this app
}

variable "ssh_public_key" {
  description = "Public SSH key to install on the EC2 instance (contents of ~/.ssh/id_rsa.pub or similar)"
  type        = string
}

variable "domain_name" {
  description = "Fully-qualified domain name for the app, e.g. t212.example.com"
  type        = string
}

variable "route53_zone_id" {
  description = "Route 53 hosted zone ID for the domain. Create this manually in the AWS console first."
  type        = string
}

variable "app_repo_url" {
  description = "Git repository URL the EC2 instance will clone on first boot"
  type        = string
  default     = "https://github.com/YOUR_ORG/YOUR_REPO.git"
}

variable "allowed_ssh_cidrs" {
  description = "CIDR blocks allowed to SSH to the instance. Restrict to your IP(s) in production."
  type        = list(string)
  default     = ["0.0.0.0/0"]  # tighten this to your own IP
}
