from datetime import date


from healthdatamodel.bmr import DEFAULT_BMR, age_from_dob, get_bmr


class TestAgFromDob:
    def test_basic(self, freezer):
        freezer.move_to("2025-06-01")
        assert age_from_dob(date(1990, 1, 1)) == 35

    def test_birthday_not_yet(self, freezer):
        freezer.move_to("2025-06-01")
        assert age_from_dob(date(1990, 12, 31)) == 34

    def test_birthday_today(self, freezer):
        freezer.move_to("2025-06-01")
        assert age_from_dob(date(1990, 6, 1)) == 35


class TestGetBmr:
    def test_returns_default_for_no_age(self):
        assert get_bmr(gender="M") == DEFAULT_BMR

    def test_returns_default_for_no_gender(self):
        assert get_bmr(age=30) == DEFAULT_BMR

    def test_returns_default_for_unknown_gender(self):
        assert get_bmr(age=30, gender="X") == DEFAULT_BMR

    def test_returns_default_for_under_18(self):
        assert get_bmr(age=17, gender="M") == DEFAULT_BMR

    def test_male_30s(self):
        result = get_bmr(age=35, gender="M")
        assert result > 0
        assert result != DEFAULT_BMR

    def test_female_40s(self):
        result = get_bmr(age=45, gender="F")
        assert result > 0
        assert result != DEFAULT_BMR

    def test_custom_default(self):
        assert get_bmr(gender="M", default=1500.0) == 1500.0
