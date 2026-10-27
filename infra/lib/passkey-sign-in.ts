import { Duration, RemovalPolicy, Stack } from "aws-cdk-lib";
import {
  AccountRecovery, CfnManagedLoginBranding, FeaturePlan, ManagedLoginVersion, OAuthScope, PasskeyUserVerification, UserPool, UserPoolClient,
  UserPoolClientIdentityProvider, UserPoolDomain,
} from "aws-cdk-lib/aws-cognito";
import { StackSettings } from "./stack-settings.js";

export interface PasskeySignIn {
  userPool: UserPool;
  appClient: UserPoolClient;
  domain: UserPoolDomain;
}

// Passkey sign-in for the PWA (slice 13): a user pool with choice-based first factors (password, passkey with user
// verification), a public app client for Authorization Code + PKCE, and a Cognito-prefix managed-login domain whose RP ID
// stays at that domain's default. Undefined when cognitoDomainPrefix is "" (sign-in off: nothing is built).
export function buildPasskeySignIn(stack: Stack, settings: StackSettings): PasskeySignIn | undefined {
  if (settings.cognitoDomainPrefix === "") {
    return undefined;
  }
  const userPool = new UserPool(stack, "UserPool", {
    featurePlan: FeaturePlan.ESSENTIALS,
    signInPolicy: { allowedFirstAuthFactors: { password: true, passkey: true } },
    passkeyUserVerification: PasskeyUserVerification.REQUIRED,
    selfSignUpEnabled: false,  // users exist only through scripts/seed_auth_users.py
    signInAliases: { email: true },
    accountRecovery: AccountRecovery.EMAIL_ONLY,
    removalPolicy: RemovalPolicy.DESTROY,
  });
  const appClient = new UserPoolClient(stack, "UserPoolClient", {
    userPool,
    generateSecret: false,  // a browser app cannot keep a secret; PKCE protects the code
    authFlows: { user: true },
    oAuth: {
      flows: { authorizationCodeGrant: true },
      scopes: [OAuthScope.OPENID, OAuthScope.EMAIL],
      callbackUrls: [settings.pwaCallbackUrl],
      logoutUrls: [settings.pwaCallbackUrl],
    },
    supportedIdentityProviders: [UserPoolClientIdentityProvider.COGNITO],
    preventUserExistenceErrors: true,
    idTokenValidity: Duration.hours(1),
    refreshTokenValidity: Duration.days(30),
    enableTokenRevocation: true,
  });
  const domain = new UserPoolDomain(stack, "UserPoolDomain", {
    userPool,
    cognitoDomain: { domainPrefix: settings.cognitoDomainPrefix },
    managedLoginVersion: ManagedLoginVersion.NEWER_MANAGED_LOGIN,
  });
  // Managed login v2 renders nothing for a client without a branding style; this one takes Cognito's default look.
  const branding = new CfnManagedLoginBranding(stack, "ManagedLoginBranding", {
    userPoolId: userPool.userPoolId,
    clientId: appClient.userPoolClientId,
    useCognitoProvidedValues: true,
  });
  branding.node.addDependency(domain);
  return { userPool, appClient, domain };
}

// The PWA's /auth-config.json (pwa/js/auth.js): sign-in on with the managed-login base URL, app client and callback, else off.
// The client id and domain resolve at deploy time.
export function buildPwaSignInConfig(settings: StackSettings, passkeySignIn: PasskeySignIn | undefined): Record<string, unknown> {
  if (passkeySignIn === undefined) {
    return { isSignInEnabled: false };
  }
  return {
    isSignInEnabled: true,
    hostedUiBaseUrl: passkeySignIn.domain.baseUrl(),
    appClientId: passkeySignIn.appClient.userPoolClientId,
    redirectUrl: settings.pwaCallbackUrl,
  };
}
