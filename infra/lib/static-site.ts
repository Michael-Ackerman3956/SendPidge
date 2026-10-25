import { RemovalPolicy, Stack } from "aws-cdk-lib";
import { Certificate, CertificateValidation } from "aws-cdk-lib/aws-certificatemanager";
import { Distribution, ResponseHeadersPolicy, ViewerProtocolPolicy } from "aws-cdk-lib/aws-cloudfront";
import { S3BucketOrigin } from "aws-cdk-lib/aws-cloudfront-origins";
import { BlockPublicAccess, Bucket, BucketEncryption } from "aws-cdk-lib/aws-s3";
import { BucketDeployment, ISource } from "aws-cdk-lib/aws-s3-deployment";

// One static site at https://<domainName>: private bucket, CloudFront with origin access control, our own certificate.
// ACM validates by DNS with no Route 53 zone, so the first deploy pauses until the validation CNAME exists at Cloudflare.
// Every deploy ships the sources, then invalidates the cache. Construct ids are <idPrefix>Bucket/Certificate/Distribution/Files.
export function buildStaticSite(stack: Stack, idPrefix: string, domainName: string, sources: ISource[]): Distribution {
  const bucket = new Bucket(stack, `${idPrefix}Bucket`, {
    blockPublicAccess: BlockPublicAccess.BLOCK_ALL,
    encryption: BucketEncryption.S3_MANAGED,
    enforceSSL: true,
    removalPolicy: RemovalPolicy.DESTROY,
    autoDeleteObjects: true,
  });
  const certificate = new Certificate(stack, `${idPrefix}Certificate`, {
    domainName,
    validation: CertificateValidation.fromDns(),
  });
  const distribution = new Distribution(stack, `${idPrefix}Distribution`, {
    defaultBehavior: {
      origin: S3BucketOrigin.withOriginAccessControl(bucket),
      viewerProtocolPolicy: ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
      // HSTS, nosniff, frame and referrer protection; the CSP stays a meta tag in index.html.
      responseHeadersPolicy: ResponseHeadersPolicy.SECURITY_HEADERS,
    },
    defaultRootObject: "index.html",
    domainNames: [domainName],
    certificate,
  });
  new BucketDeployment(stack, `${idPrefix}Files`, {
    sources,
    destinationBucket: bucket,
    distribution,
    distributionPaths: ["/*"],
  });
  return distribution;
}
