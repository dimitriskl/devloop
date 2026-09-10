# RMA: αποτελέσματα ελέγχου EBS

Ημερομηνία: 09/09/2026. Φάση: ανάλυση μόνο. Οι απαντήσεις των ομάδων στα ερωτήματα δεν έχουν ακόμη παραληφθεί σε αυτή τη συνομιλία. Τα παρακάτω είναι δικά μας επαληθευμένα ευρήματα και όχι απαντήσεις της υποστήριξης.

## Αποτέλεσμα

Η αποθηκευμένη σύνδεση EBS λειτουργεί για ανάγνωση των μεταδεδομένων της συγκεκριμένης όψης RMA. Ανακτήθηκαν 66 στήλες με δηλωμένους τύπους και λεζάντες, η ρίζα ESFILineItem και οι δύο παράμετροι αναζήτησης. Αυτό απαντά μέρος των τεχνικών ερωτημάτων. Δεν αποδεικνύει τις αντιστοιχίσεις Magento, τους υποχρεωτικούς περιορισμούς της εισαγωγής, τις οικονομικές έννοιες ή την ασφαλή επαναποστολή.

Δεν εκτελέστηκε CreateEshopRMA, ScrollerCommand ή ExecuteScrollerCommand. Δεν δημιουργήθηκαν templates, παραστατικά ή αλλαγές εφαρμογής/βάσης. Δεν αποθηκεύτηκαν κλειδιά, κωδικοί ή δεδομένα πελατών στα αρχεία αποτελεσμάτων.

## Πηγές και πραγματικές κλήσεις

- Η σύνδεση επαληθεύτηκε με read-only SELECT στη βάση μέσω eCv2Local, μετά από έλεγχο του πραγματικού schema του dbo.econnector_entersoft_ebs_connections.
- Μοναδική μη διαγραμμένη εγγραφή: Entersoft EBS Production, ενεργή, Environment=1 (Production), AuthMode=1 (StaticApiKeyHeader), ID 272531a4-415f-4ecc-b388-6908c33076aa, CompanyId 104f2d7c-6d30-408a-a294-0b9e6d512953.
- Base URL: https://api.entersoft.gr/api/rpc. Το κρυπτογραφημένο κλειδί διαβάστηκε και αποκρυπτογραφήθηκε στη μνήμη βάσει του πραγματικού ConnectionStringEncryptionService. Μεταφέρθηκε στον HTTP helper μόνο μέσω stdin.
- Οι ορισμοί των API ελέγχθηκαν στο [δημοσιευμένο Swagger της Entersoft](https://eswebapi-next.azurewebsites.net/swagger/docs/api3.0). Το web εργαλείο δεν άνοιξε τη διεύθυνση, αλλά η άμεση HTTPS ανάγνωση πέτυχε. Το σχετικό υποσύνολο αποθηκεύτηκε στο Swagger.json. Οι ορισμοί Swagger είναι γενική τεκμηρίωση και όχι απόδειξη εγκατάστασης/επιτυχίας του συγκεκριμένου αυτοματισμού στο production.
- Οι HTTP κλήσεις χρησιμοποίησαν GET, κανονική επαλήθευση TLS, απόρριψη redirects, timeout 20 δευτερολέπτων και όριο ανάγνωσης 2 MB. Δεν έγινε αυτόματη επανάληψη μετά το timeout.

| Διαδρομή μετά το /api/rpc/ | Αποτέλεσμα | Αρχείο |
| --- | --- | --- |
| PublicQueryInfo/WEB_Scrolls/fnky_EshopRMA | HTTP 200, πλήρες σώμα 41.285 bytes | PublicQueryInfo.json |
| PublicQueryLayout/WEB_Scrolls/fnky_EshopRMA | HTTP 200, πλήρες σώμα 41.285 bytes | PublicQueryLayout.json |
| SimpleScroller/WEB_Scrolls/fnky_EshopRMA | TimeoutError στο όριο του client· δεν καταγράφηκε HTTP αποτέλεσμα ή γραμμές | SimpleScroller.json |
| FetchOdsTableInfo/CSFunkyIncomingRMA | HTTP 404, invalid-table-id | StagingTableInfo.json |

Τα δύο επιτυχημένα metadata responses είναι ίδια ως JSON περιεχόμενο. Το 404 σημαίνει ότι το συγκεκριμένο ID δεν αναγνωρίστηκε από αυτό το ODS metadata endpoint· δεν αποδεικνύει απουσία του SQL πίνακα. Το timeout δεν αποδεικνύει ότι η όψη είναι κενή ή ανύπαρκτη. Δεν επαληθεύτηκαν πραγματικές RMA γραμμές, server-side pagination ή εγγύηση διακοπής της server-side ανάγνωσης μετά το client timeout.

## Τι μάθαμε για την όψη

- ID: fnky_EshopRMA.
- Τίτλος: Νέο Αίτημα RMA EShop Altex.
- RootTable και SelectedMasterTable: ESFILineItem.
- QueryID: WEB_Scrolls\fnky_EshopRMA\fnky_EshopRMA_Q1.esq. Πρόκειται για αναφορά αρχείου· το ίδιο το query δεν ανακτήθηκε.
- Παράμετροι αναζήτησης: TradeAccountCode (System.String) και RMADate (Entersoft.Framework.Platform.ESDateRange), και οι δύο με Required=false. Αυτό αφορά τα φίλτρα ανάγνωσης, όχι την υποχρεωτικότητα των πεδίων εισαγωγής.
- Δεν επιστράφηκαν ενότητες DefaultValue ή EnumItem. Η απουσία τους δεν αποδεικνύει ότι η εισαγωγή δεν εφαρμόζει defaults ή business enums.
- Όλες οι 66 στήλες υπάρχουν στο VIEW-COLUMNS.md. Δεν έχουμε μέσω αυτής της απάντησης SQL μήκη, precision/scale, nullability ή πλήρη import validation.

| Πεδίο | Δηλωμένος τύπος στη live όψη | Σημασία για την ανάλυση |
| --- | --- | --- |
| MagentoID, MagentoRMAID, RMACode, RelatedOrderMagentoID, RelatedOrderMagentoOrderID | String | Δεν μετατρέπουμε αυθαίρετα όλα τα IDs σε αριθμούς ή αφαιρούμε αρχικά μηδενικά |
| RMACode | String | Λεζάντα «Κωδικός παραγγελίας Magento». Χρειάζεται επιβεβαίωση έναντι της ονομασίας RMA· δεν αρκεί για mapping |
| MagentoOrderLineID | String | Λεζάντα «Magento Order LineID». Παραμένει η αντίφαση με την παράμετρο/στήλη MagentoRMALineID του XML |
| CustomerAPIID | Decimal | Ο δηλωμένος τύπος διαφέρει από το string consumerId του JSON· απαιτείται συμφωνημένη μετατροπή |
| ItemCSAPIID, LineNumber | Int32 | Δηλωμένοι ακέραιοι, χωρίς απόδειξη business mapping ή εύρους πραγματικών τιμών |
| Item_reason, Item_resolution, Item_condition | String | Οι κωδικοί είναι αριθμητικοί στα JSON παραδείγματα, αλλά η live όψη δηλώνει String |
| Quantity, ShippingCost, Total_QTY, Price, DiscountPercentage, BasePrice, VATValue, TotalValue | Decimal | Δεν αρκεί integer schema· ΦΠΑ, εκπτώσεις και επίπεδο ποσού χρειάζονται επιβεβαίωση |
| TotalValue | Decimal | Λεζάντα «Τελική αξία γραμμής». Δεν επιτρέπεται να αντιγράψουμε αυτομάτως το order-level customAttributes.totalValue σε κάθε γραμμή |
| SHIPPING_METHOD | String | Λεζάντα «Τρόπος αποστολής επιστροφής», συμβατή με τη διάκριση return/order shipping στα παραδείγματα |
| Invoice, VisitorOrder | Byte | Δεν δηλώνονται Boolean στη live όψη |
| RMADate, Payment_date | DateTime | Δεν τεκμηριώνεται timezone/offset από τον τύπο |
| BankGID, Iban, Exchange_sku | String | Δεν αποδεικνύεται ότι είναι πάντοτε προαιρετικά |

Οι λεζάντες είναι επαληθευμένα metadata, όχι εγγυημένη επιχειρησιακή σημασία. Δεν επιλύουμε τις αντιφάσεις επιλέγοντας τη βολικότερη ερμηνεία.

## Αντιστοίχιση στα ερωτήματα προς EBS

| Ερώτημα | Κατάσταση | Τι καλύφθηκε / τι περιμένουμε |
| --- | --- | --- |
| EBS-01: όψη, types, required/defaults | Μερικώς απαντημένο | Live layout 66 στηλών, root table και φίλτρα. Παραμένουν query/export, nullability, μήκη, numeric precision και import απαιτήσεις/defaults |
| EBS-02: MagentoRMAID / RMACode | Ανοικτό με νέα ένδειξη | Και τα δύο String. Η λεζάντα του RMACode αναφέρεται σε παραγγελία. Χρειάζεται ρητή αντιστοίχιση από EBS/Magento |
| EBS-03: order-line έναντι RMA-line ID | Ανοικτό με σύγκρουση ενδείξεων | Η live λεζάντα λέει order line, αλλά το XML αποθηκεύει την τιμή σε MagentoRMALineID. Καμία επιλογή ID δεν εγκρίθηκε |
| EBS-04: order links, DocType/Type | Μερικώς διερευνημένο | Τα links και DocType/Type δηλώνονται String. Το XML ελέγχει DocType="Order" σε document logic. Δεν έχει επιβεβαιωθεί πλήρης κανόνας συμπλήρωσης |
| EBS-05: resolutions / παραστατικά / mixed lines | Μερικώς διερευνημένο από XML | Η δεύτερη document διαδρομή έχει έκφραση Order και resolution 5 ή 6. Βρέθηκαν document InternationalID CS.ΠΣΠΕ και CS.ΠΔΣΕ. Δεν αποδεικνύονται η επιχειρησιακή σημασία τους, πλήρης κατάλογος resolutions, mixed-line συμπεριφορά ή εκτέλεση χρηματικής επιστροφής |
| EBS-06: ποσά / ποσότητες / ΦΠΑ | Ανοικτό με νέα ένδειξη | Decimal types και λεζάντα TotalValue «Τελική αξία γραμμής». Δεν επιβεβαιώθηκαν ΦΠΑ/εκπτώσεις, τιμή μονάδας έναντι συνόλου, κατανομή μεταφορικών ή υπολογισμοί |
| EBS-07: πληρωμή / τράπεζα / guest | Μερικώς διερευνημένο από XML | Τύποι πεδίων διαθέσιμοι. Το XML περιέχει έλεγχο IBAN, χειρισμό EmptyStringToDBNull για BankGID, αναζήτηση πελάτη μέσω αρχικής παραγγελίας και fallback CustomerINF. Δεν θεωρούμε αυτά πλήρες συμβόλαιο guest/refund χωρίς επιβεβαίωση |
| EBS-08: endpoint / request / response | Μερικώς απαντημένο | Το Swagger τεκμηριώνει POST /api/rpc/ScrollerCommand/ και /api/rpc/ExecuteScrollerCommand/, request με ScrollerID/CommandID/ScrollerDataset και response με ScrollerDataset/TargetDatasets/Variables. Δεν πραγματοποιήθηκε invocation· το συγκεκριμένο επιτυχές RMA αποτέλεσμα και τα επιστρεφόμενα document IDs παραμένουν άγνωστα |
| EBS-09: duplicates / partial failures | Ανοικτό | Υπάρχουν XML έλεγχοι εύρεσης εγγράφων και πεδία transaction στο γενικό Swagger. Δεν αποτελούν απόδειξη idempotency, πλήρους rollback ή ασφαλούς replay μετά από timeout |

Δεν έχει κλείσει πλήρως κάποιο από τα σύνθετα EBS ερωτήματα. Το τεχνικό μέρος της EBS-01 και το γενικό API σχήμα της EBS-08 είναι πλέον πολύ πιο συγκεκριμένα. Δεν χρειάζεται να ζητάμε ξανά απλή επιβεβαίωση ότι υπάρχει προσβάσιμο layout.

## Πρόσθετη παρατήρηση XML για ποσότητα

Στον δεύτερο document κλάδο υπάρχει Assign Column="Quantity" με ConstantValue 1 (περίπου γραμμές 2725–2729 του παρεχόμενου XML). Δεν έχει αναλυθεί/δοκιμαστεί όλος ο μηχανισμός πολλαπλασιασμού/ομαδοποίησης ώστε να δηλωθεί λάθος. Χρειάζεται επιβεβαίωση της συμπεριφοράς για quantity μεγαλύτερο του 1· όλα τα τωρινά JSON παραδείγματα έχουν quantity 1 ανά γραμμή.

## Συνέχιση

Διαβάστε αυτό το αρχείο μετά το αρχικό HANDOFF.md. Υπερισχύει μόνο ως προς τα νέα live ευρήματα και την παλαιότερη δήλωση ότι δεν είχαν γίνει καθόλου EBS κλήσεις. Οι περιορισμοί analysis-only, τα αρχικά ερωτήματα και όλες οι μη επιβεβαιωμένες αντιστοιχίσεις παραμένουν.

Όταν φτάσουν οι απαντήσεις, αντιπαραβάλετε ιδίως τις EBS-02/03/06 με τις πραγματικές λεζάντες και types. Μην εκλάβετε το 200 του metadata endpoint ως επιτυχία εισαγωγής RMA. Μην εκτελέσετε automation, ακόμη και με OnlyPrepareTargetDatasets, ως μέθοδο εξερεύνησης χωρίς ξεχωριστά επιβεβαιωμένη συμπεριφορά και εξουσιοδότηση.

Τα αρχικά JSON/XML παραμένουν στα docs/eshop/rma και docs/eshop/CreateEshopRMA.xml. Ο πλήρης κατάλογος types είναι στο VIEW-COLUMNS.md και τα dated HTTP αποδεικτικά στα ομώνυμα JSON αυτού του φακέλου. Δεν τροποποιήθηκαν τα ήδη σταλμένα ερωτήματα.
