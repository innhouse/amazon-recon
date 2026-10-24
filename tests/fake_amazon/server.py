"""Tiny stand-in for Amazon's Your Orders page, used to test the invoice fetcher end to end."""
import http.server
import socketserver
import sys
from urllib.parse import parse_qs, urlparse

ORDER = "701-0000001-0000001"
SINGLE = "701-0000002-0000002"
PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/gp/your-account/order-history":
            q = parse_qs(u.query).get("search", [""])[0]
            body = f"""<html><head><title>Your Orders</title></head><body>
            <h1>Search results</h1>
            {'<div class="order">Order no. ' + SINGLE + ' <a href="#" onclick="document.getElementById(\'pop2\').style.display=\'block\';return false;">Invoice</a>'
             '<div id="pop2" style="display:none"><a href="/docs/summary">Printable Order Summary</a><a href="/docs/single.pdf">Invoice</a><a href="/req">Request Invoice</a></div></div>' if q == SINGLE else ''}
            {'<div class="order">Order no. ' + ORDER + ' <a href="#" id="inv" data-a-popover=\'{"url": "/gp/shared-cs/ajax/invoice/invoice.html?orderId=' + ORDER + '"}\' onclick="document.getElementById(\'pop\').style.display=\'block\';return false;">Invoice</a>'
             '<div id="pop" style="display:none"><a href="/docs/summary">Printable Order Summary</a>'
             '<a href="/docs/1.pdf">Invoice 1</a><a href="/req">Request Invoice</a></div></div>' if q == ORDER else '<p>No orders found</p>'}
            </body></html>""".encode()
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(body)
        elif u.path == "/gp/shared-cs/ajax/invoice/invoice.html":
            body = b'<ul><li><a href="/docs/summary">Printable Order Summary</a></li><li><a href="/docs/1.pdf">Invoice 1</a></li><li><a href="/docs/2.pdf">Invoice 2</a></li><li><a href="/req">Request Invoice</a></li></ul>'
            self.send_response(200); self.send_header("Content-Type", "text/html"); self.end_headers(); self.wfile.write(body)
        elif u.path.startswith("/docs/") and u.path.endswith(".pdf"):
            self.send_response(200); self.send_header("Content-Type", "application/pdf"); self.end_headers(); self.wfile.write(PDF)
        else:
            self.send_response(404); self.end_headers()


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    with socketserver.TCPServer(("127.0.0.1", port), H) as s:
        s.serve_forever()
