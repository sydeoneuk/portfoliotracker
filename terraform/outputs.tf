output "elastic_ip" {
  description = "Static public IP address of the EC2 instance"
  value       = aws_eip.app.public_ip
}

output "public_dns" {
  description = "Application URL"
  value       = "https://${var.domain_name}"
}

output "ssh_command" {
  description = "SSH command to connect to the instance"
  value       = "ssh -i ~/.ssh/YOUR_KEY ec2-user@${aws_eip.app.public_ip}"
}

output "instance_id" {
  description = "EC2 instance ID"
  value       = aws_instance.app.id
}
