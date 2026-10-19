"""Sign-in (Cognito passkeys), the HTTP API, and the app's hosting (private S3 behind CloudFront)."""

import json
from pathlib import Path

from aws_cdk import Aws, CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cloudfront as cloudfront
from aws_cdk import aws_cloudfront_origins as origins
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_s3_deployment as s3deploy
from aws_cdk import aws_sns as sns
from cdk_nag import NagPackSuppression, NagSuppressions, RegexAppliesTo
from constructs import Construct

DEV_ORIGIN = "http://localhost:5173"  # only allowed when the stack is synthesized with -c devOrigin=true

ROUTES = (
    (apigw.HttpMethod.GET, "/devices"),
    (apigw.HttpMethod.GET, "/devices/{id}"),
    (apigw.HttpMethod.POST, "/devices/{id}/commands"),
)


class Web(Construct):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        site_dir: str,
        topic: sns.ITopic,
    ) -> None:
        super().__init__(scope, construct_id)
        region = Stack.of(self).region

        # ---- hosting: private bucket, served only through CloudFront
        bucket = s3.Bucket(
            self,
            "SiteBucket",
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL,
            encryption=s3.BucketEncryption.S3_MANAGED,
            enforce_ssl=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            removal_policy=RemovalPolicy.DESTROY,
            auto_delete_objects=True,
        )
        auth_prefix = f"homehub-{Aws.ACCOUNT_ID}"
        auth_url = f"https://{auth_prefix}.auth.{region}.amazoncognito.com"
        cognito_api = f"https://cognito-idp.{region}.amazonaws.com"  # SRP and passkey calls from the browser
        dev_origins = [DEV_ORIGIN] if self.node.try_get_context("devOrigin") in (True, "true") else []
        headers = cloudfront.ResponseHeadersPolicy(
            self,
            "SecurityHeaders",
            security_headers_behavior=cloudfront.ResponseSecurityHeadersBehavior(
                content_security_policy=cloudfront.ResponseHeadersContentSecurityPolicy(
                    content_security_policy=(
                        "default-src 'none'; script-src 'self'; style-src 'self'; font-src 'self'; img-src 'self' data:; "
                        "manifest-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; "
                        f"connect-src 'self' https://*.execute-api.{region}.amazonaws.com {auth_url} {cognito_api}; "
                        "upgrade-insecure-requests"
                    ),
                    override=True,
                ),
                strict_transport_security=cloudfront.ResponseHeadersStrictTransportSecurity(
                    access_control_max_age=Duration.days(365), include_subdomains=True, override=True
                ),
                content_type_options=cloudfront.ResponseHeadersContentTypeOptions(override=True),
                frame_options=cloudfront.ResponseHeadersFrameOptions(
                    frame_option=cloudfront.HeadersFrameOption.DENY, override=True
                ),
                referrer_policy=cloudfront.ResponseHeadersReferrerPolicy(
                    referrer_policy=cloudfront.HeadersReferrerPolicy.NO_REFERRER, override=True
                ),
            ),
            custom_headers_behavior=cloudfront.ResponseCustomHeadersBehavior(
                custom_headers=[
                    cloudfront.ResponseCustomHeader(
                        header="Permissions-Policy",
                        value="camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()",
                        override=True,
                    ),
                    cloudfront.ResponseCustomHeader(header="Cross-Origin-Opener-Policy", value="same-origin", override=True),
                ]
            ),
        )
        distribution = cloudfront.Distribution(
            self,
            "Site",
            default_behavior=cloudfront.BehaviorOptions(
                origin=origins.S3BucketOrigin.with_origin_access_control(bucket),  # pyright: ignore[reportArgumentType]
                viewer_protocol_policy=cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
                response_headers_policy=headers,
            ),
            default_root_object="index.html",
            # The app routes in the browser: unknown paths load the app.
            error_responses=[
                cloudfront.ErrorResponse(http_status=code, response_http_status=200, response_page_path="/index.html")
                for code in (403, 404)
            ],
            price_class=cloudfront.PriceClass.PRICE_CLASS_100,
        )
        site_url = f"https://{distribution.distribution_domain_name}"

        # ---- sign-in: passkeys (Face ID / Touch ID) first; password as backup; no self sign-up
        pool = cognito.UserPool(
            self,
            "UserPool",
            self_sign_up_enabled=False,
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            password_policy=cognito.PasswordPolicy(
                min_length=12,
                require_lowercase=True,
                require_uppercase=True,
                require_digits=True,
                require_symbols=True,
                temp_password_validity=Duration.days(3),
            ),
            mfa=cognito.Mfa.OPTIONAL,
            mfa_second_factor=cognito.MfaSecondFactor(otp=True, sms=False),
            sign_in_policy=cognito.SignInPolicy(
                allowed_first_auth_factors=cognito.AllowedFirstAuthFactors(password=True, passkey=True)
            ),
            passkey_user_verification=cognito.PasskeyUserVerification.REQUIRED,
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            feature_plan=cognito.FeaturePlan.ESSENTIALS,
            deletion_protection=True,
            removal_policy=RemovalPolicy.RETAIN,
        )
        cfn_pool: cognito.CfnUserPool = pool.node.default_child  # type: ignore[assignment]
        cfn_pool.web_authn_factor_configuration = "MULTI_FACTOR_WITH_USER_VERIFICATION"  # a passkey counts as MFA
        client = pool.add_client(
            "WebClient",
            auth_flows=cognito.AuthFlow(user=True, user_srp=True),
            prevent_user_existence_errors=True,
            generate_secret=False,  # public browser client (authorization code + PKCE)
            access_token_validity=Duration.hours(1),
            id_token_validity=Duration.hours(1),
            refresh_token_validity=Duration.days(30),
            enable_token_revocation=True,
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[cognito.OAuthScope.OPENID, cognito.OAuthScope.EMAIL],
                callback_urls=[f"{o}/" for o in [site_url, *dev_origins]],
                logout_urls=[f"{o}/" for o in [site_url, *dev_origins]],
            ),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
        )
        # Managed login v2 is needed for passkeys; its style must exist before the page switches to v2.
        branding = cognito.CfnManagedLoginBranding(
            self,
            "LoginBranding",
            user_pool_id=pool.user_pool_id,
            client_id=client.user_pool_client_id,
            use_cognito_provided_values=False,
            settings=json.loads((Path(__file__).parent / "login" / "branding.json").read_text()),  # dark
        )
        domain = pool.add_domain(
            "LoginDomain",
            cognito_domain=cognito.CognitoDomainOptions(domain_prefix=auth_prefix),
            managed_login_version=cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
        )
        domain.node.add_dependency(branding)

        # ---- API: every route needs a signed-in user
        fn = lambda_.Function(
            self,
            "Api",
            runtime=lambda_.Runtime.PYTHON_3_14,
            architecture=lambda_.Architecture.ARM_64,
            handler="api.handler.handler",
            code=lambda_.Code.from_asset("backend", exclude=["**/__pycache__"]),
            memory_size=256,
            timeout=Duration.seconds(10),
            log_group=logs.LogGroup(self, "ApiLogs", retention=logs.RetentionDays.TWO_WEEKS),
        )  # no table access yet: the mock API stores nothing; grant it when the adapters keep tokens
        # A burst of crashes (3 in 5 minutes) or throttled calls emails the alert topic.
        for metric_name, metric in (("Errors", fn.metric_errors), ("Throttles", fn.metric_throttles)):
            metric(period=Duration.minutes(5)).create_alarm(
                self,
                f"Api{metric_name}",
                threshold=3,
                evaluation_periods=1,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description=f"The app's API Lambda reported {metric_name.lower()}",
            ).add_alarm_action(cw_actions.SnsAction(topic))  # pyright: ignore[reportArgumentType]
        api = apigw.HttpApi(
            self,
            "HttpApi",
            create_default_stage=False,
            default_authorizer=authorizers.HttpUserPoolAuthorizer("Cognito", pool, user_pool_clients=[client]),  # pyright: ignore[reportArgumentType]
            cors_preflight=apigw.CorsPreflightOptions(
                allow_origins=[site_url, *dev_origins],
                allow_methods=[apigw.CorsHttpMethod.GET, apigw.CorsHttpMethod.POST],
                allow_headers=["Authorization", "Content-Type"],
                max_age=Duration.hours(1),
            ),
        )
        integration = integrations.HttpLambdaIntegration("ApiIntegration", fn)  # pyright: ignore[reportArgumentType]
        for method, path in ROUTES:
            api.add_routes(path=path, methods=[method], integration=integration)
        access_logs = logs.LogGroup(self, "ApiAccessLogs", retention=logs.RetentionDays.TWO_WEEKS)
        stage = apigw.HttpStage(
            self,
            "Stage",
            http_api=api,
            stage_name="$default",
            auto_deploy=True,
            throttle=apigw.ThrottleSettings(rate_limit=10, burst_limit=20),
        )
        cfn_stage: apigw.CfnStage = stage.node.default_child  # type: ignore[assignment]
        cfn_stage.access_log_settings = apigw.CfnStage.AccessLogSettingsProperty(
            destination_arn=access_logs.log_group_arn,
            format='{"requestId":"$context.requestId","route":"$context.routeKey","status":"$context.status"}',
        )
        api_url = stage.url.rstrip("/")

        # ---- app files plus config.json with this stack's API and sign-in settings
        s3deploy.BucketDeployment(
            self,
            "DeploySite",
            destination_bucket=bucket,
            sources=[
                s3deploy.Source.asset(site_dir),
                s3deploy.Source.json_data(
                    "config.json",
                    {"apiUrl": api_url, "authUrl": auth_url, "clientId": client.user_pool_client_id, "region": region},
                ),
            ],
            distribution=distribution,
            distribution_paths=["/*"],
            memory_limit=256,
        )

        CfnOutput(Stack.of(self), "SiteUrl", value=site_url)
        CfnOutput(Stack.of(self), "ApiUrl", value=api_url)
        CfnOutput(Stack.of(self), "UserPoolId", value=pool.user_pool_id)
        self._suppress(fn, pool, distribution, bucket)

    def _suppress(
        self,
        fn: lambda_.Function,
        pool: cognito.UserPool,
        distribution: cloudfront.Distribution,
        bucket: s3.Bucket,
    ) -> None:
        def reason(rule: str, why: str, applies_to: list | None = None) -> NagPackSuppression:
            return NagPackSuppression(id=rule, reason=why, applies_to=applies_to)

        logs_policy = "Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
        NagSuppressions.add_resource_suppressions(
            fn,
            [
                reason("AwsSolutions-IAM4", "Basic execution role only writes this function's logs", [logs_policy]),
            ],
            apply_to_children=True,
        )
        NagSuppressions.add_resource_suppressions(
            pool,
            [
                reason("AwsSolutions-COG2", "Passkeys with user verification are the primary factor; TOTP optional"),
                reason("AwsSolutions-COG3", "Advanced security (Plus plan) costs extra; sign-up is closed"),
                reason("AwsSolutions-COG8", "Plus plan costs extra per user; Essentials has passkeys"),
            ],
            apply_to_children=True,
        )
        NagSuppressions.add_resource_suppressions(
            distribution,
            [
                reason("AwsSolutions-CFR1", "Personal app; no geo restriction needed"),
                reason("AwsSolutions-CFR2", "WAF costs ~$5+/month, over budget; API is behind sign-in and throttled"),
                reason("AwsSolutions-CFR3", "Access logs cost extra; CloudFront metrics suffice for a personal app"),
                reason("AwsSolutions-CFR4", "Default CloudFront domain and certificate (no custom domain yet)"),
            ],
        )
        NagSuppressions.add_resource_suppressions(
            bucket, [reason("AwsSolutions-S1", "Static app files only; access logging costs extra")]
        )
        # CDK's own helper Lambdas (bucket deployment, auto-delete): their runtime and policies are CDK's.
        NagSuppressions.add_stack_suppressions(
            Stack.of(self),
            [
                reason("AwsSolutions-IAM4", "CDK-managed helper Lambdas use the basic execution role", [logs_policy]),
                reason(
                    "AwsSolutions-IAM5",
                    "CDK bucket deployment needs object-level wildcards on its own buckets",
                    [RegexAppliesTo(regex=r"/^(Action::s3:.*|Resource::.*\*)$/g")],
                ),  # fmt: skip
                reason("AwsSolutions-L1", "CDK-managed helper Lambdas pick their own runtime"),
            ],
        )
