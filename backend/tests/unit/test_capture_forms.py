# Turkish words use the dotless i on purpose.
# ruff: noqa: RUF001

from __future__ import annotations

import pytest

from kalkan.scanning.form_patterns import button_intent, form_patterns, input_kind, normalize


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("cardNumber", "card number"),
        ("card_number", "card number"),
        ("Card-Number", "card number"),
        ("İŞLEMİ DOĞRULA", "islemi dogrula"),
        ("YATIR", "yatir"),
        ("Wpłać", "wplac"),
        ("Войти", "воити"),
    ],
)
def test_normalize(text: str, expected: str) -> None:
    assert normalize(text) == expected


@pytest.mark.parametrize(
    ("attributes", "kind"),
    [
        ({"input_type": "password", "texts": ["pwd"]}, "password"),
        ({"input_type": "text", "texts": ["user_password"]}, "password"),
        ({"input_type": "text", "texts": ["", "", "Şifre"]}, "password"),
        ({"input_type": "text", "texts": ["hasło"]}, "password"),
        ({"input_type": "text", "texts": ["пароль"]}, "password"),
        ({"input_type": "text", "autocomplete": "cc-number", "texts": ["card"]}, "card_number"),
        ({"input_type": "tel", "texts": ["cardNumber"]}, "card_number"),
        ({"input_type": "text", "placeholder": "1234 5678 9012 3456"}, "card_number"),
        ({"input_type": "text", "texts": ["numer karty"]}, "card_number"),
        ({"input_type": "text", "texts": ["cvv"]}, "card_cvv"),
        ({"input_type": "text", "autocomplete": "cc-csc"}, "card_cvv"),
        ({"input_type": "text", "texts": ["", "", "Expiry date"]}, "card_expiry"),
        ({"input_type": "text", "placeholder": "MM/YY"}, "card_expiry"),
        ({"input_type": "text", "texts": ["iban"]}, "iban"),
        ({"input_type": "text", "placeholder": "PL61 1090 1014 0000 0712 1981 2874"}, "iban"),
        ({"input_type": "tel", "texts": ["msisdn"]}, "phone"),
        ({"input_type": "text", "texts": ["telefon"]}, "phone"),
        ({"input_type": "text", "autocomplete": "one-time-code"}, "otp"),
        ({"input_type": "text", "texts": ["sms_code"]}, "otp"),
        ({"input_type": "email", "texts": ["login"]}, "email"),
        ({"input_type": "text", "autocomplete": "username", "texts": ["login"]}, "username"),
        ({"input_type": "text", "texts": ["kullanıcı adı"]}, "username"),
        ({"input_type": "number", "texts": ["amount"]}, "amount"),
        ({"input_type": "text", "texts": ["search"]}, ""),
        ({"input_type": "text", "texts": ["passport"]}, ""),
        ({"input_type": "checkbox", "texts": ["remember"]}, ""),
    ],
)
def test_input_kinds(attributes: dict[str, object], kind: str) -> None:
    assert input_kind(**attributes) == kind  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("Deposit and log in", "deposit"),
        ("Para Yatır", "deposit"),
        ("YATIRIM YAP", "deposit"),
        ("Wpłać teraz", "deposit"),
        ("Внести депозит", "deposit"),
        ("Пополнить счёт", "deposit"),
        ("Jetzt einzahlen", "deposit"),
        ("Log in", "login"),
        ("Oturum Aç", "login"),
        ("OTURUM AÇ", "login"),
        ("Zaloguj się", "login"),
        ("Войти", "login"),
        ("Вход", "login"),
        ("Anmelden", "login"),
        ("Sign up", "register"),
        ("Kayıt Ol", "register"),
        ("Zarejestruj się", "register"),
        ("Регистрация", "register"),
        ("Detaylar", ""),
        ("Read more", ""),
        ("", ""),
    ],
)
def test_button_intents(text: str, intent: str) -> None:
    assert button_intent(text) == intent


def test_the_prompt_words_are_covered() -> None:
    for word in ("deposit", "yatır", "wpłać", "внести", "login", "giriş", "zaloguj", "войти"):
        assert button_intent(word) in {"deposit", "login"}, word


def test_patterns_load_every_kind() -> None:
    kinds = [rule.kind for rule in form_patterns().fields]
    assert kinds[0] == "password"
    expected = {"card_number", "card_cvv", "card_expiry", "iban", "phone", "email", "otp"}
    assert expected <= set(kinds)
    assert [intent for intent, _ in form_patterns().buttons] == ["deposit", "login", "register"]
