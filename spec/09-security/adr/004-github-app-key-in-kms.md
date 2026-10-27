# ADR-09-004: GitHub App private key imported into KMS; only the broker signs

- **Status:** Accepted for the WS-09 design (2026-10-24). Refines ADR-011 ("rotated per WS-09").
- **Constraints:** D-21, D-24

## Context

The App private key can mint installation tokens for every installation of the App: the most
valuable credential in Replicant. ADR-011 makes `github-token-broker` its sole holder. The
remaining question is whether the broker holds the PEM (in memory, loaded from a secret store) or
only the ability to sign.

## Options

1. **PEM in Secrets Manager**, loaded by the broker at startup. Any broker RCE or memory disclosure
   leaks a key that does not expire ("Private keys do not expire and instead need to be manually
   revoked" [verified:
   https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/managing-private-keys-for-github-apps,
   2026-10-24]).
2. **Key imported into KMS as non-exportable RSA_2048 SIGN_VERIFY**; the broker calls `kms:Sign`
   (`RSASSA_PKCS1_V1_5_SHA_256`) for each App JWT. KMS supports importing RSA private key material,
   wrapped with `RSA_AES_KEY_WRAP_SHA_256` [verified:
   https://docs.aws.amazon.com/kms/latest/developerguide/importing-keys.html and
   https://docs.aws.amazon.com/kms/latest/developerguide/importing-keys-get-public-key-and-token.html,
   2026-10-24]. GitHub explicitly recommends a sign-only key vault [verified: GitHub page above].
3. CloudHSM. Same property as option 2 at much higher cost and operational load.

## Decision

Option 2, with the import ceremony, rotation, and key policy in [secrets.md](../secrets.md) §4 and
[kms-key-policy.github-app.json](../policies/iam/kms-key-policy.github-app.json).

## Consequences

- A broker compromise yields signing capability only while the attacker controls the broker; the
  key cannot be exfiltrated. Recovery is "fix the broker", not "rotate everywhere". Rotation is
  still done (the attacker may have minted tokens valid ≤ 1 h).
- Each JWT costs one KMS `Sign` call; with an 8-minute JWT cache, demand is negligible against the
  1,000 RPS RSA quota [verified: https://docs.aws.amazon.com/kms/latest/developerguide/requests-per-second.html, 2026-10-24].
- The PEM exists briefly during the ceremony; the ceremony is two-person and on a destroyed
  workstation.
- Imported key material is the customer's responsibility for durability: GitHub can always issue a
  new key, so no offline copy is kept.

## Revisit when

- GitHub supports registering a customer-generated public key or workload OIDC for Apps (then
  generate the key inside KMS and never export it at all).
