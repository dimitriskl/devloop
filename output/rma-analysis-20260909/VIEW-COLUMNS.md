# Live RMA view columns

Retrieved 2026-09-09 from GET PublicQueryLayout/WEB_Scrolls/fnky_EshopRMA (HTTP 200). These are published view layout types/captions, not SQL nullability, string length, precision or proof of accepted import values.

| Column | Declared type | Caption |
| --- | --- | --- |
| DocGID | Guid | DocGID |
| DocType | String | DocType |
| MagentoID | String | MagentoID |
| MagentoRMAID | String | MagentoRMAID |
| RMACode | String | Κωδικός παραγγελίας Magento |
| RelatedOrderMagentoID | String | Σχετικό MagentoID παρ/λίας |
| RelatedOrderMagentoOrderID | String | Σχετικό MagentoOrderID παρ/λίας |
| RMADate | DateTime | Ημερομηνία RMA |
| CustomerAPIID | Decimal | Magento CustomerID |
| TradeAccountCode | String | Κωδικός πελάτη |
| CompanyName | String | CompanyName |
| CustomerLastName | String | Επώνυμο πελάτη |
| CustomerFirstName | String | Όνομα πελάτη |
| EMailAddress | String | Email πελάτη |
| Mobile1 | String | Κινητό τηλέφωνο πελάτη |
| TaxRegistrationNumber | String | Α.Φ.Μ. πελάτη |
| DOY | String | Δ.Ο.Υ |
| Activity | String | Επάγγελμα |
| RMAComments | String | Σχόλια RMA |
| DeliveryComments | String | DeliveryComments |
| CustomerAddressAPIIDb | String | Magento AddressID για την Διεύθυνση Τιμολόγησης |
| address1_b | String | Διεύθυνση τιμολόγησης 1 |
| address2_b | String | Διεύθυνση τιμολόγησης 2 |
| postalcode_b | String | ΤΚ διεύθυνσης τιμολόγησης |
| city_b | String | Πόλη διεύθυνσης τιμολόγησης |
| area_b | String | Περιοχή διεύθυνσης τιμολόγησης |
| Country_b | String | Χώρα διεύθυνσης τιμολόγησης |
| TelephoneB | String | Σταθερό τηλέφωνο διεύθυνσης τιμολόγησης |
| CustomerAddressAPIIDs | String | Magento AddressID για την Διεύθυνση παραλαβής |
| address1_s | String | Διεύθυνση παραλαβής 1 |
| address2_s | String | Διεύθυνση παραλαβής 2 |
| postalcode_s | String | ΤΚ διεύθυνσης παραλαβής |
| city_s | String | Πόλη διεύθυνσης παραλαβής |
| area_s | String | Περιοχή διεύθυνσης παραλαβής |
| Country_s | String | Χώρα διεύθυνσης παραλαβής |
| TelephoneS | String | Σταθερό τηλέφωνο διεύθυνσης παραλαβής |
| ShippingCost | Decimal | Αξία μεταφορικών |
| Total_QTY | Decimal | Total QTY |
| RMAReturnReason | String | Λόγος επιστροφής RMA |
| SHIPPING_METHOD | String | Τρόπος αποστολής επιστροφής |
| MagentoOrderLineID | String | Magento Order LineID |
| Type | String | Τύπος γραμμής |
| LineNumber | Int32 | Αρ.Γραμμής |
| ItemCSAPIID | Int32 | Simple ID είδους |
| LineRMAAReasoning | String | Λόγος επιστροφής γραμμής |
| Quantity | Decimal | Ποσότητα γραμμής |
| Price | Decimal | Τιμή είδους |
| DiscountPercentage | Decimal | DiscountPercentage |
| BasePrice | Decimal | BasePrice |
| VATValue | Decimal | Αξία ΦΠΑ γραμμής |
| TotalValue | Decimal | Τελική αξία γραμμής |
| Item_reason | String | Item_reason |
| Item_resolution | String | Item_resolution |
| Item_condition | String | Item_condition |
| Iban | String | Iban |
| PaymentMethodCode | String | Payment MethodCode |
| Invoice | Byte | Invoice |
| VisitorOrder | Byte | Visitor Order |
| Gender | String | Gender |
| Exchange_sku | String | Exchange_sku |
| BankGID | String | BankGID |
| PayerEmail | String | PayerEmail |
| ReferenceNumber | String | ReferenceNumber |
| Payment_date | DateTime | Payment_date |
| LockerID | String | Locker ID |
| CARRIER | String | CARRIER |
