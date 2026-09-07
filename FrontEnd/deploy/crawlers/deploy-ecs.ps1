[CmdletBinding()]
param(
  [string]$Profile = "crawling-observer-rds",
  [string]$Region = "ap-northeast-2",
  [string]$DbInstanceIdentifier = "crawling-observer-mysql",
  [string[]]$SubnetIds = @("subnet-0bb56d79cf3278b76", "subnet-0cb0f752fe48a791a"),
  [string]$BootstrapObjectKey = "bootstrap/overseas-history-queue.db",
  [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-Native {
  param(
    [Parameter(Mandatory)][string]$Program,
    [Parameter(Mandatory)][string[]]$CommandArgs,
    [switch]$Capture
  )
  if ($Capture) {
    $output = & $Program @CommandArgs 2>&1
    if ($LASTEXITCODE -ne 0) { throw "$Program failed: $($output -join [Environment]::NewLine)" }
    return ($output -join "`n").Trim()
  }
  & $Program @CommandArgs
  if ($LASTEXITCODE -ne 0) { throw "$Program failed with exit code $LASTEXITCODE" }
}

function Invoke-Aws {
  param([Parameter(Mandatory)][string[]]$CommandArgs, [switch]$Capture)
  $common = @("--profile", $Profile, "--region", $Region, "--no-cli-pager")
  return Invoke-Native -Program "aws" -CommandArgs ($CommandArgs + $common) -Capture:$Capture
}

function Get-AwsText {
  param([Parameter(Mandatory)][string[]]$CommandArgs)
  return Invoke-Aws -CommandArgs ($CommandArgs + @("--output", "text")) -Capture
}

function Ensure-LogGroup {
  param([Parameter(Mandatory)][string]$Name)
  $found = Get-AwsText @("logs", "describe-log-groups", "--log-group-name-prefix", $Name,
    "--query", "logGroups[?logGroupName=='$Name'].logGroupName | [0]")
  if (-not $found -or $found -eq "None") {
    Invoke-Aws @("logs", "create-log-group", "--log-group-name", $Name)
  }
  Invoke-Aws @("logs", "put-retention-policy", "--log-group-name", $Name, "--retention-in-days", "30")
}

function Ensure-EcrRepository {
  param([Parameter(Mandatory)][string]$Name)
  $found = $null
  try {
    $found = Get-AwsText @("ecr", "describe-repositories", "--repository-names", $Name,
      "--query", "repositories[0].repositoryUri")
  } catch {
    $found = $null
  }
  if (-not $found -or $found -eq "None") {
    return Get-AwsText @("ecr", "create-repository", "--repository-name", $Name,
      "--image-scanning-configuration", "scanOnPush=true", "--query", "repository.repositoryUri")
  }
  return $found
}

function Ensure-Role {
  param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$TrustJson)
  $roleArn = $null
  try {
    $roleArn = Get-AwsText @("iam", "get-role", "--role-name", $Name, "--query", "Role.Arn")
  } catch {
    $roleArn = $null
  }
  if (-not $roleArn -or $roleArn -eq "None") {
    $roleArn = Get-AwsText @("iam", "create-role", "--role-name", $Name,
      "--assume-role-policy-document", $TrustJson, "--query", "Role.Arn")
  } else {
    Invoke-Aws @("iam", "update-assume-role-policy", "--role-name", $Name,
      "--policy-document", $TrustJson)
  }
  return $roleArn
}

function Ensure-SecurityGroup {
  param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$Description,
    [Parameter(Mandatory)][string]$VpcId)
  $groupId = Get-AwsText @("ec2", "describe-security-groups", "--filters",
    "Name=group-name,Values=$Name", "Name=vpc-id,Values=$VpcId",
    "--query", "SecurityGroups[0].GroupId")
  if (-not $groupId -or $groupId -eq "None") {
    $groupId = Get-AwsText @("ec2", "create-security-group", "--group-name", $Name,
      "--description", $Description, "--vpc-id", $VpcId, "--query", "GroupId")
    Invoke-Aws @("ec2", "create-tags", "--resources", $groupId,
      "--tags", "Key=Name,Value=$Name", "Key=Project,Value=nexus-risk")
  }
  return $groupId
}

function Ensure-SecurityGroupIngress {
  param([Parameter(Mandatory)][string]$TargetGroupId, [Parameter(Mandatory)][int]$Port,
    [Parameter(Mandatory)][string]$SourceGroupId)
  $rules = (Invoke-Aws @("ec2", "describe-security-group-rules", "--filters",
    "Name=group-id,Values=$TargetGroupId", "--output", "json") -Capture | ConvertFrom-Json).SecurityGroupRules
  $found = $rules | Where-Object {
    $referencedGroup = $_.PSObject.Properties["ReferencedGroupInfo"]
    -not $_.IsEgress -and $_.IpProtocol -eq "tcp" -and $_.FromPort -eq $Port -and
      $_.ToPort -eq $Port -and $referencedGroup -and $referencedGroup.Value.GroupId -eq $SourceGroupId
  } | Select-Object -First 1
  if (-not $found) {
    Invoke-Aws @("ec2", "authorize-security-group-ingress", "--group-id", $TargetGroupId,
      "--protocol", "tcp", "--port", "$Port", "--source-group", $SourceGroupId)
  }
}

function Ensure-EcsService {
  param([Parameter(Mandatory)][string]$Name, [Parameter(Mandatory)][string]$TaskDefinition,
    [Parameter(Mandatory)][string]$Cluster, [Parameter(Mandatory)][string]$NetworkConfiguration)
  $status = Get-AwsText @("ecs", "describe-services", "--cluster", $Cluster, "--services", $Name,
    "--query", "services[0].status")
  if (-not $status -or $status -eq "None" -or $status -eq "INACTIVE") {
    Invoke-Aws @("ecs", "create-service", "--cluster", $Cluster, "--service-name", $Name,
      "--task-definition", $TaskDefinition, "--desired-count", "0", "--launch-type", "FARGATE",
      "--platform-version", "LATEST", "--network-configuration", $NetworkConfiguration,
      "--deployment-configuration", "deploymentCircuitBreaker={enable=true,rollback=true}")
  } else {
    Invoke-Aws @("ecs", "update-service", "--cluster", $Cluster, "--service", $Name,
      "--task-definition", $TaskDefinition, "--desired-count", "0", "--force-new-deployment")
  }
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$accountId = Get-AwsText @("sts", "get-caller-identity", "--query", "Account")
$dbInfo = (Invoke-Aws @("rds", "describe-db-instances", "--db-instance-identifier", $DbInstanceIdentifier,
  "--query", "DBInstances[0].{Endpoint:Endpoint.Address,Vpc:DBSubnetGroup.VpcId,Secret:MasterUserSecret.SecretArn,RdsSecurityGroup:VpcSecurityGroups[0].VpcSecurityGroupId}",
  "--output", "json") -Capture) | ConvertFrom-Json
if (-not $dbInfo.Secret) { throw "RDS managed master-user secret is not available." }

$clusterName = "nexus-risk-crawlers"
$domesticRepository = "nexus-risk-domestic-crawler"
$overseasRepository = "nexus-risk-overseas-crawler"
$domesticLogGroup = "/ecs/nexus-risk-domestic"
$overseasLogGroup = "/ecs/nexus-risk-overseas"
$stateBucket = "nexus-risk-crawler-state-$accountId-$Region"
$tag = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss")

$domesticRepositoryUri = Ensure-EcrRepository $domesticRepository
$overseasRepositoryUri = Ensure-EcrRepository $overseasRepository
Ensure-LogGroup $domesticLogGroup
Ensure-LogGroup $overseasLogGroup
Invoke-Aws @("ecs", "create-cluster", "--cluster-name", $clusterName,
  "--settings", "name=containerInsights,value=enabled", "--tags", "key=Project,value=nexus-risk")

$trustJson = (@{
  Version = "2012-10-17"
  Statement = @(@{ Effect = "Allow"; Principal = @{ Service = "ecs-tasks.amazonaws.com" }; Action = "sts:AssumeRole" })
} | ConvertTo-Json -Depth 8 -Compress)
$executionRoleName = "nexus-risk-ecs-execution-role"
$taskRoleName = "nexus-risk-crawler-task-role"
$executionRoleArn = Ensure-Role $executionRoleName $trustJson
$taskRoleArn = Ensure-Role $taskRoleName $trustJson
Invoke-Aws @("iam", "attach-role-policy", "--role-name", $executionRoleName,
  "--policy-arn", "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy")
$executionSecretPolicy = (@{
  Version = "2012-10-17"
  Statement = @(@{ Effect = "Allow"; Action = @("secretsmanager:GetSecretValue"); Resource = $dbInfo.Secret })
} | ConvertTo-Json -Depth 8 -Compress)
Invoke-Aws @("iam", "put-role-policy", "--role-name", $executionRoleName,
  "--policy-name", "ReadRdsManagedSecret", "--policy-document", $executionSecretPolicy)
$taskPolicy = (@{
  Version = "2012-10-17"
  Statement = @(
    @{ Effect = "Allow"; Action = @("rds:DescribeDBInstances"); Resource = "*" },
    @{ Effect = "Allow"; Action = @("secretsmanager:GetSecretValue"); Resource = $dbInfo.Secret },
    @{ Effect = "Allow"; Action = @("s3:GetObject"); Resource = "arn:aws:s3:::$stateBucket/$BootstrapObjectKey" }
  )
} | ConvertTo-Json -Depth 8 -Compress)
Invoke-Aws @("iam", "put-role-policy", "--role-name", $taskRoleName,
  "--policy-name", "CrawlerRuntimeAccess", "--policy-document", $taskPolicy)

$crawlerSecurityGroup = Ensure-SecurityGroup "nexus-risk-ecs-crawlers" "Nexus Risk ECS crawler tasks" $dbInfo.Vpc
$efsSecurityGroup = Ensure-SecurityGroup "nexus-risk-efs" "Nexus Risk EFS mount targets" $dbInfo.Vpc
Ensure-SecurityGroupIngress $dbInfo.RdsSecurityGroup 3306 $crawlerSecurityGroup
Ensure-SecurityGroupIngress $efsSecurityGroup 2049 $crawlerSecurityGroup

$fileSystemId = Get-AwsText @("efs", "describe-file-systems", "--creation-token", "nexus-risk-crawler-state",
  "--query", "FileSystems[0].FileSystemId")
if (-not $fileSystemId -or $fileSystemId -eq "None") {
  $fileSystemId = Get-AwsText @("efs", "create-file-system", "--creation-token", "nexus-risk-crawler-state",
    "--encrypted", "--performance-mode", "generalPurpose", "--throughput-mode", "bursting",
    "--tags", "Key=Name,Value=nexus-risk-crawler-state", "Key=Project,Value=nexus-risk",
    "--query", "FileSystemId")
}
$fileSystemAvailable = $false
for ($attempt = 0; $attempt -lt 40; $attempt++) {
  $fileSystemState = Get-AwsText @("efs", "describe-file-systems", "--file-system-id", $fileSystemId,
    "--query", "FileSystems[0].LifeCycleState")
  if ($fileSystemState -eq "available") { $fileSystemAvailable = $true; break }
  if ($fileSystemState -eq "error" -or $fileSystemState -eq "deleted") {
    throw "EFS $fileSystemId entered state $fileSystemState"
  }
  Start-Sleep -Seconds 3
}
if (-not $fileSystemAvailable) { throw "Timed out waiting for EFS $fileSystemId" }
$existingMountTargets = (Invoke-Aws @("efs", "describe-mount-targets", "--file-system-id", $fileSystemId,
  "--output", "json") -Capture | ConvertFrom-Json).MountTargets
foreach ($subnetId in $SubnetIds) {
  $mountTarget = $existingMountTargets | Where-Object { $_.SubnetId -eq $subnetId } | Select-Object -First 1
  if (-not $mountTarget) {
    Invoke-Aws @("efs", "create-mount-target", "--file-system-id", $fileSystemId,
      "--subnet-id", $subnetId, "--security-groups", $efsSecurityGroup)
  }
}

$bucketExists = $true
try { Invoke-Aws @("s3api", "head-bucket", "--bucket", $stateBucket) } catch { $bucketExists = $false }
if (-not $bucketExists) {
  Invoke-Aws @("s3api", "create-bucket", "--bucket", $stateBucket,
    "--create-bucket-configuration", "LocationConstraint=$Region")
}
Invoke-Aws @("s3api", "put-bucket-versioning", "--bucket", $stateBucket,
  "--versioning-configuration", "Status=Enabled")
Invoke-Aws @("s3api", "put-bucket-encryption", "--bucket", $stateBucket,
  "--server-side-encryption-configuration", '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}')

if (-not $SkipBuild) {
  Invoke-Native "docker" @("build", "--pull", "-f", "deploy/crawlers/Dockerfile.domestic",
    "-t", "nexus-risk-domestic-crawler:local", ".")
  Invoke-Native "docker" @("build", "--pull", "-f", "overseas-news-crawler/Dockerfile",
    "-t", "nexus-risk-overseas-crawler:local", "overseas-news-crawler")
}
$loginPassword = Invoke-Aws @("ecr", "get-login-password") -Capture
$loginPassword | & docker login --username AWS --password-stdin "$accountId.dkr.ecr.$Region.amazonaws.com"
if ($LASTEXITCODE -ne 0) { throw "docker login failed" }
$domesticImage = "${domesticRepositoryUri}:$tag"
$overseasImage = "${overseasRepositoryUri}:$tag"
Invoke-Native "docker" @("tag", "nexus-risk-domestic-crawler:local", $domesticImage)
Invoke-Native "docker" @("tag", "nexus-risk-overseas-crawler:local", $overseasImage)
Invoke-Native "docker" @("push", $domesticImage)
Invoke-Native "docker" @("push", $overseasImage)

$commonEnvironment = @(
  @{ name = "MYSQL_HOST"; value = $dbInfo.Endpoint },
  @{ name = "MYSQL_PORT"; value = "3306" },
  @{ name = "MYSQL_DATABASE"; value = "nexus_risk_domestic_news" },
  @{ name = "MYSQL_SSL_CA_PATH"; value = "/opt/nexus/certs/ap-northeast-2-bundle.pem" }
)
$commonSecrets = @(
  @{ name = "MYSQL_USER"; valueFrom = "$($dbInfo.Secret):username::" },
  @{ name = "MYSQL_PASSWORD"; valueFrom = "$($dbInfo.Secret):password::" }
)
function New-DomesticContainer {
  param([string]$Name, [int]$Cpu, [int]$Memory, [string[]]$Command)
  return @{
    name = $Name; image = $domesticImage; essential = $true; cpu = $Cpu; memoryReservation = $Memory
    command = $Command; environment = $commonEnvironment; secrets = $commonSecrets
    logConfiguration = @{ logDriver = "awslogs"; options = @{
      "awslogs-group" = $domesticLogGroup; "awslogs-region" = $Region; "awslogs-stream-prefix" = $Name
    } }
  }
}
$domesticTask = @{
  family = "nexus-risk-domestic-crawlers"; networkMode = "awsvpc"
  requiresCompatibilities = @("FARGATE"); cpu = "2048"; memory = "4096"
  executionRoleArn = $executionRoleArn; taskRoleArn = $taskRoleArn
  runtimePlatform = @{ cpuArchitecture = "X86_64"; operatingSystemFamily = "LINUX" }
  containerDefinitions = @(
    (New-DomesticContainer "publisher-history" 768 1536 @("node", "--experimental-strip-types",
      "scripts/crawl-publisher-history-to-mysql.mjs", "--years", "10", "--continuous",
      "--host-delay-ms", "2000", "--concurrency", "8", "--discovery-concurrency", "6",
      "--max-documents-per-domain", "100", "--max-archive-pages-per-domain", "100",
      "--max-articles-per-run", "20000", "--max-articles-per-domain-run", "5000",
      "--progress-every", "50", "--round-pause-seconds", "10")),
    (New-DomesticContainer "hellot-range" 256 512 @("/bin/sh", "-lc",
      'while true; do node --experimental-strip-types scripts/crawl-hellot-range-to-mysql.mjs --start 10000 --end 114676 --delay-ms 1000 --concurrency 2 --retries 4 --progress-every 25; status=$?; if [ $status -eq 0 ]; then sleep 86400; else sleep 60; fi; done')),
    (New-DomesticContainer "publisher-sitemaps" 512 1024 @("node", "--experimental-strip-types",
      "scripts/crawl-publisher-sitemaps-to-mysql.mjs", "--watch", "--interval-minutes", "60",
      "--concurrency", "5", "--limit-per-source", "2500", "--retries", "3", "--progress-every", "50")),
    (New-DomesticContainer "active-publishers" 512 1024 @("node", "--experimental-strip-types",
      "scripts/crawl-active-publisher-sitemaps-to-mysql.mjs", "--watch", "--interval-minutes", "360",
      "--limit-per-domain", "300", "--max-sitemap-roots", "3", "--max-sitemap-documents", "5",
      "--max-sitemap-children", "3", "--concurrency", "8", "--discovery-concurrency", "8",
      "--host-delay-ms", "1500", "--retries", "2", "--progress-every", "50"))
  )
}
$domesticTaskArn = Get-AwsText @("ecs", "register-task-definition", "--cli-input-json",
  ($domesticTask | ConvertTo-Json -Depth 20 -Compress), "--query", "taskDefinition.taskDefinitionArn")

$bootstrapS3Uri = "s3://$stateBucket/$BootstrapObjectKey"
$overseasTask = @{
  family = "nexus-risk-overseas-crawler"; networkMode = "awsvpc"
  requiresCompatibilities = @("FARGATE"); cpu = "1024"; memory = "2048"
  executionRoleArn = $executionRoleArn; taskRoleArn = $taskRoleArn
  runtimePlatform = @{ cpuArchitecture = "X86_64"; operatingSystemFamily = "LINUX" }
  containerDefinitions = @(@{
    name = "overseas-history"; image = $overseasImage; essential = $true; cpu = 1024; memoryReservation = 1536
    environment = @(
      @{ name = "AWS_REGION"; value = $Region }, @{ name = "AWS_DEFAULT_REGION"; value = $Region },
      @{ name = "BOOTSTRAP_S3_URI"; value = $bootstrapS3Uri },
      @{ name = "RDS_INSTANCE_ID"; value = $DbInstanceIdentifier },
      @{ name = "RDS_DATABASE"; value = "crawling_observer" },
      @{ name = "START_DATE"; value = "2016-09-02" }, @{ name = "END_DATE"; value = "2026-09-02" }
    )
    mountPoints = @(@{ sourceVolume = "crawler-state"; containerPath = "/data"; readOnly = $false })
    logConfiguration = @{ logDriver = "awslogs"; options = @{
      "awslogs-group" = $overseasLogGroup; "awslogs-region" = $Region; "awslogs-stream-prefix" = "overseas-history"
    } }
  })
  volumes = @(@{ name = "crawler-state"; efsVolumeConfiguration = @{
    fileSystemId = $fileSystemId; rootDirectory = "/"; transitEncryption = "ENABLED"
  } })
}
$overseasTaskArn = Get-AwsText @("ecs", "register-task-definition", "--cli-input-json",
  ($overseasTask | ConvertTo-Json -Depth 20 -Compress), "--query", "taskDefinition.taskDefinitionArn")

$networkConfiguration = "awsvpcConfiguration={subnets=[$($SubnetIds -join ',')],securityGroups=[$crawlerSecurityGroup],assignPublicIp=ENABLED}"
Ensure-EcsService "nexus-risk-domestic-crawlers" $domesticTaskArn $clusterName $networkConfiguration
Ensure-EcsService "nexus-risk-overseas-crawler" $overseasTaskArn $clusterName $networkConfiguration

[pscustomobject]@{
  Cluster = $clusterName
  DomesticTaskDefinition = $domesticTaskArn
  OverseasTaskDefinition = $overseasTaskArn
  DomesticImage = $domesticImage
  OverseasImage = $overseasImage
  CrawlerSecurityGroup = $crawlerSecurityGroup
  EfsFileSystem = $fileSystemId
  StateBucket = $stateBucket
  BootstrapS3Uri = $bootstrapS3Uri
} | ConvertTo-Json -Depth 4
