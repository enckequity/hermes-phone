from hermes_phone.dialer import describe_twilio_error


class FakeTwilioError(Exception):
    def __init__(self, code, status, msg):
        super().__init__(msg)
        self.code, self.status, self.msg = code, status, msg


def test_describes_code_status_message_and_hint():
    text = describe_twilio_error(FakeTwilioError(21219, 400, "The number is unverified."))
    assert "code 21219" in text and "HTTP 400" in text and "unverified" in text
    assert "Verified Caller IDs" in text


def test_plain_exception_is_just_the_type():
    assert describe_twilio_error(ValueError()) == "ValueError"


def test_kyc_error_is_not_reported_as_bad_credentials():
    text = describe_twilio_error(FakeTwilioError(
        20003, 401, "Unable to create record: Primary compliance profile is not approved. "
                    "Please refer to documentation and complete the KYC process in Trust Hub"))
    assert "Trust Hub" in text and "check TWILIO_ACCOUNT_SID" not in text
