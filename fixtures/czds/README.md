# CZDS fixtures

`xyz.zone.gz`: a small gzip zone in the CZDS format (lower case, tab separated, absolute
owner names). It holds 120 delegated names, each with two NS records, plus the apex SOA and
NS records, one glue A record and one DS record that the parser must skip. The brand-like
names are `mintera-login.xyz`, `m1ntera.xyz`, `login-rnintera.xyz`, `xn--minter-8nf.xyz`
(`minterа` with a Cyrillic `а`) and `mintera-verify.xyz` (matches for the keyword `mintera`)
and `eramint.xyz` and `mint.xyz` (no match). The glue A record belongs to
`ns1.mintera-login.xyz` and the DS record to `mintera-offers.xyz`. All names are fictional.
