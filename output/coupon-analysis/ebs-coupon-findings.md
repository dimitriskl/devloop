# EBS promotional coupon discovery

Verified on 9 September 2026. Scope: promotional coupons for Shopify. No business records, templates, or configuration were changed.

## Result

The existing eConnector connection works for read-only EBS inspection. We retrieved live voucher and promotion-profile metadata, the complete four-row voucher-state lookup, and a bounded sample from the exact view supplied by the EBS team. Several technical questions can now be removed from the original request. Business meanings and synchronization guarantees remain open.

## Connection and existing configuration

- Application database: configured diagnostics connection `eCv2Local`, database `eConnectorV2`.
- Company: Altex, `104f2d7c-6d30-408a-a294-0b9e6d512953`.
- Active connection: `Entersoft EBS Production`, `272531a4-415f-4ecc-b388-6908c33076aa`.
- Configured base URL: `https://api.entersoft.gr/api/rpc`.
- Authentication: configured static API-key header. The encrypted key was read from the database and decrypted in process using the application's verified encryption implementation. No key or plaintext credentials were written into the evidence files.
- Existing template: `PROD 03.Vouchers`, ID `a8090360-9484-4677-8046-d2b545467ab4`, configured route `vouchers`.
- The current template's production schema declares `DiscVoucherValue` and `DiscVoucherPercentage` as `int`. The live EBS metadata declares both `System.Decimal`. This mismatch must be resolved before relying on fractional values. This probe does not establish that any existing production records have been truncated or corrupted.

## Verified HTTP results

All requests below used GET under the configured account. The operation definitions were inspected in [Entersoft's published Swagger](https://eswebapi-next.azurewebsites.net/swagger/docs/api3.0). No automation/command execution endpoint was called.

| Path beneath `/api/rpc` | Result | Evidence |
|---|---|---|
| `SimpleScroller/ESFIVoucher/ESFIVoucher_Def` | HTTP 200; voucher rows returned | `SimpleScroller.json` |
| `FetchOdsTableInfo/ESFIVoucher` | HTTP 200; 50 column definitions | `FetchOdsTableInfo.json` |
| `FetchOdsTableInfo/ESFIVoucherPromotionProfile` | HTTP 200; profile column definitions | `VoucherProfileInfo.json` |
| `FetchOdsTableInfo/ESFIZVoucherState` | HTTP 200; state column definitions | `VoucherStateInfo.json` |
| `FetchStdZoom/ESFIZVoucherState` | HTTP 200; four state records | `VoucherStates.json` |
| `PublicQueryInfo/ESFIVoucher/ESFIVoucher_Def` | HTTP 404, `invalid-public-query` | `PublicQueryInfo.json` |
| `PublicQueryLayout/ESFIVoucher/ESFIVoucher_Def` | HTTP 404, `invalid-public-query` | `PublicQueryLayout.json` |

The Public Query failures apply to those exact routes and this account. They do not prove the view is absent: the SimpleScroller call succeeded. Whether a different published Public Query exists remains unverified.

A metadata relation probe using `FetchOdsMasterRelationsInfo/ESFIVoucherPromotionProfile` returned `invalid-column-id`. It provides no evidence that the profile has no relationships.

## Actual view sample

The unfiltered view response exceeded the probe's 2,000,000-byte response bound. The probe parsed the first 50 complete rows from the bounded response prefix and retained five sanitized examples. It did not enumerate the entire dataset, verify server-side pagination, or establish population-wide distributions or uniqueness.

The five retained examples contain:

| Field | Observed value |
|---|---|
| `VoucherType` | `0` |
| `DiscVoucherValue` | `6.0` |
| `DiscVoucherPercentage` | `0.0` |
| `VoucherState` | `2 / Ενεργό` |
| `Inactive` | `0` |
| `PromotionProfileCode` | `CT6-2027` |
| `fCompanyCode` | `001` |
| `RegistrationDate` | `2026-09-02T21:00:00` |
| `ExpirationDate` | `2027-09-02T21:00:00` |

Currency is not returned. The value must not be labelled EUR without verification. The timestamps have no offset; timezone and intended business-day boundaries remain unverified. Seeing `VoucherType=0` with a fixed discount is evidence of those records, not proof of the full enum or that every type-0 record follows the same rule.

The sampled rows expose 26 fields, including `GID`, `Code`, `Barcode`, `Description`, discount fields, state, profile, dates and company. They also expose `login_date`, whose business meaning has not been established. Full coupon codes, customer information and voucher identifiers were not retained in the sample evidence.

## Source fields now established

| Required concept | Verified EBS evidence | Remaining decision |
|---|---|---|
| Stable record reference | `GID`, non-null `System.Guid` in metadata; returned by view | Lifetime stability and cross-system identity policy |
| Customer-entered coupon code | Both `Code` and `Barcode` exist and are returned; metadata sizes are 50 | Which field is authoritative for promotional coupons; uniqueness/reuse rules |
| Display title | `Description` exists and is nullable | Whether it is the intended title; handling missing descriptions |
| Discount amount | `DiscVoucherValue`, non-null `System.Decimal` | Currency, precedence and interpretation when both discount fields are populated |
| Discount percentage | `DiscVoucherPercentage`, non-null `System.Decimal`; EBS type `ESPERCENTDISCOUNT` | Numeric scale; no percentage example was retained |
| Instrument classification | `Type`, integer, choice type `Entersoft.ERP.Financials.Enums.VoucherType, ESFIEnums`; projected as `VoucherType` | Complete numeric enum mapping and exact promotional-coupon filter |
| Status | `Inactive` plus `fVoucherStateCode`; view returns combined `VoucherState` | Online mapping, precedence, expiry transitions and handling used coupons |
| Validity | Nullable `RegistrationDate` and `ExpirationDate` | Whether registration is activation; null rules; timezone semantics |
| Modification tracking | `ESDModified` exists in live table metadata | Not returned by sampled view; coverage of rule changes, expiry and deletion is unverified |
| Campaign/profile | `fVoucherPromotionProfileGID` exists; view returns `PromotionProfileCode` | Relation to campaign/pool and which rules must be projected |

The state lookup is directly verified:

| EBS code | Description |
|---|---|
| `1` | Αρχικό |
| `2` | Ενεργό |
| `3` | Χρησιμοποιήθηκε |
| `4` | Ακυρώθηκε |

Every state lookup row has `Inactive=0`. That is lookup-record metadata and must not be confused with a voucher's own `Inactive` value. State `3` is particularly relevant to shared usage limits; it does not independently establish that every coupon is single-use.

## Restrictions may be stored beyond the voucher row

Live `ESFIVoucherPromotionProfile` metadata contains `Named`, `fCampaignGID`, `fItemListGID`, `fDiscountGroupCode`, `DiscountGroupProcessType`, `fInvoicePolicyActionGID`, `fTradeAccountListGID`, `fTradeAccountSiteListGID` and `fConditionGID`.

These are verified columns and useful leads. Their names do not prove their business meanings, actual values, related table mappings, precedence, or Shopify representation. The sampled voucher view does not expose these rule definitions. We must not treat an omitted rule as unrestricted eligibility.

Neither the voucher metadata nor the sampled view provides an explicitly identified `isDeleted`/`deletedAt` contract. Custom flags and fields exist, but their use cannot be inferred. `ESUCreated` is a string field; it is not the required modification timestamp.

## Remaining work before templates can be finalized

1. Confirm the complete coupon type/code/discount interpretation, with a percentage example and any case where both value fields are populated.
2. Resolve promotion-profile restrictions, permitted channels, combination behavior, currency and shared usage rules.
3. Confirm state and validity behavior, including initial/used/inactive states and timestamp interpretation.
4. Establish a complete extract with supported pagination/filtering and change/deletion reporting, including changes to related profile rules.
5. Decide whether online usage must update EBS business state or is only optional reporting. If business state must change, obtain the approved EBS operation and retry/reversal contract.
6. Resolve delivery ownership: Shopify integration reads eConnector data, or eConnector calls Shopify. No choice was assumed during this investigation.

See `questions-for-ebs-el.md` for the shorter follow-up request based on these findings. No executable synchronization template has been created.
