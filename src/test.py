from methods.audit import audit_website
import pprint

result = audit_website("https://unlost-nine.vercel.app")
pprint.pprint(result)