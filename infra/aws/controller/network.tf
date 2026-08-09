data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "main" {
  cidr_block           = "10.42.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "${local.name_prefix}-vpc"
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${local.name_prefix}-igw"
  }
}

resource "aws_subnet" "controller" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.42.0.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true

  tags = {
    Name = "${local.name_prefix}-controller"
  }
}

resource "aws_route_table" "controller" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name = "${local.name_prefix}-controller"
  }
}

resource "aws_route_table_association" "controller" {
  subnet_id      = aws_subnet.controller.id
  route_table_id = aws_route_table.controller.id
}

resource "aws_security_group" "controller" {
  name_prefix = "${local.name_prefix}-controller-"
  description = "No ingress; controller administration uses AWS Systems Manager"
  vpc_id      = aws_vpc.main.id
  ingress     = []

  egress {
    description = "Outbound package, Moodle, AWS API, and agent traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name_prefix}-controller"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_subnet" "lab" {
  vpc_id                  = aws_vpc.main.id
  cidr_block              = "10.42.16.0/20"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true

  tags = {
    Name = "${local.name_prefix}-lab"
    Role = "lab"
  }
}

resource "aws_route_table_association" "lab" {
  subnet_id      = aws_subnet.lab.id
  route_table_id = aws_route_table.controller.id
}

resource "aws_security_group" "lab" {
  name_prefix = "${local.name_prefix}-lab-"
  description = "No ingress; ephemeral labs use AWS Systems Manager"
  vpc_id      = aws_vpc.main.id
  ingress     = []

  egress {
    description = "Outbound task, package, Moodle, and AWS API traffic"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${local.name_prefix}-lab"
    Role = "lab"
  }

  lifecycle {
    create_before_destroy = true
  }
}
