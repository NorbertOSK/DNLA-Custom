import unittest

import dlnacast


class TimeFormatTests(unittest.TestCase):
    def test_to_hms_zero(self):
        self.assertEqual(dlnacast.to_hms(0), "0:00:00")

    def test_to_hms_full(self):
        self.assertEqual(dlnacast.to_hms(3661), "1:01:01")

    def test_to_hms_negative_clamps(self):
        self.assertEqual(dlnacast.to_hms(-5), "0:00:00")

    def test_from_hms_full(self):
        self.assertEqual(dlnacast.from_hms("1:01:01"), 3661)

    def test_from_hms_minutes_seconds(self):
        self.assertEqual(dlnacast.from_hms("02:03"), 123)

    def test_from_hms_fractional(self):
        self.assertEqual(dlnacast.from_hms("0:00:10.500"), 10)

    def test_from_hms_invalid(self):
        self.assertEqual(dlnacast.from_hms("garbage"), 0)
        self.assertEqual(dlnacast.from_hms(None), 0)

    def test_roundtrip(self):
        self.assertEqual(dlnacast.from_hms(dlnacast.to_hms(7325)), 7325)


class SoapParseTests(unittest.TestCase):
    ENVELOPE = (
        '<?xml version="1.0"?>'
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
        's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
        "<s:Body>"
        '<u:SetAVTransportURI xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">'
        "<InstanceID>0</InstanceID>"
        "<CurrentURI>http://192.168.1.5:8080/video.mp4</CurrentURI>"
        "<CurrentURIMetaData></CurrentURIMetaData>"
        "</u:SetAVTransportURI>"
        "</s:Body></s:Envelope>"
    )

    def test_parse_action_and_args(self):
        action, args = dlnacast.parse_soap(self.ENVELOPE)
        self.assertEqual(action, "SetAVTransportURI")
        self.assertEqual(args["InstanceID"], "0")
        self.assertEqual(args["CurrentURI"], "http://192.168.1.5:8080/video.mp4")
        self.assertEqual(args["CurrentURIMetaData"], "")

    def test_parse_empty_body(self):
        empty = (
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
            "<s:Body></s:Body></s:Envelope>"
        )
        action, args = dlnacast.parse_soap(empty)
        self.assertIsNone(action)
        self.assertEqual(args, {})


class SsdpMessageTests(unittest.TestCase):
    def test_response_contains_required_headers(self):
        response = dlnacast.build_ssdp_response(
            "upnp:rootdevice", "uuid:x::upnp:rootdevice",
            "http://10.0.0.2:8895/description.xml")
        self.assertTrue(response.startswith("HTTP/1.1 200 OK\r\n"))
        self.assertIn("ST: upnp:rootdevice\r\n", response)
        self.assertIn("USN: uuid:x::upnp:rootdevice\r\n", response)
        self.assertIn("LOCATION: http://10.0.0.2:8895/description.xml\r\n", response)
        self.assertTrue(response.endswith("\r\n\r\n"))

    def test_notify_alive(self):
        message = dlnacast.build_ssdp_notify(
            "upnp:rootdevice", "uuid:x::upnp:rootdevice",
            "http://10.0.0.2:8895/description.xml", "ssdp:alive")
        self.assertTrue(message.startswith("NOTIFY * HTTP/1.1\r\n"))
        self.assertIn("NTS: ssdp:alive\r\n", message)

    def test_targets_cover_device_and_services(self):
        targets = dict(dlnacast.ssdp_targets("uuid:abc"))
        self.assertIn("upnp:rootdevice", targets)
        self.assertIn("uuid:abc", targets)
        self.assertIn("urn:schemas-upnp-org:device:MediaRenderer:1", targets)
        self.assertIn("urn:schemas-upnp-org:service:AVTransport:1", targets)


class ScpdTests(unittest.TestCase):
    def test_scpds_are_valid_xml(self):
        from xml.etree import ElementTree as ET
        for name, builder in dlnacast.SCPD_BUILDERS.items():
            ET.fromstring(builder())


if __name__ == "__main__":
    unittest.main()
