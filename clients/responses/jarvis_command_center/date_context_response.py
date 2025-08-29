from typing import List, Optional
from pydantic import BaseModel


class DateInfo(BaseModel):
    """Represents a single date with its ISO format and UTC start of day"""
    date: str
    utc_start_of_day: str


class WeekendDay(DateInfo):
    """Represents a weekend day with additional day information"""
    day: str


class CurrentDate(BaseModel):
    """Represents the current date and time information"""
    date: str
    date_iso: str
    time: str
    datetime: str
    weekday: str
    weekday_number: int
    utc_start_of_day: str


class RelativeDates(BaseModel):
    """Represents relative dates (tomorrow, yesterday, etc.)"""
    tomorrow: DateInfo
    yesterday: DateInfo
    day_after_tomorrow: DateInfo
    day_before_yesterday: DateInfo


class WeekendDates(BaseModel):
    """Represents weekend date ranges"""
    this_weekend: List[WeekendDay]
    next_weekend: List[WeekendDay]
    last_weekend: List[WeekendDay]


class WeekDates(BaseModel):
    """Represents week date ranges"""
    this_week: List[WeekendDay]
    next_week: List[WeekendDay]
    last_week: List[WeekendDay]


class MonthDates(BaseModel):
    """Represents month date ranges"""
    this_month: List[DateInfo]
    next_month: List[DateInfo]
    last_month: List[DateInfo]


class YearDates(BaseModel):
    """Represents year date ranges"""
    this_year: List[DateInfo]
    next_year: List[DateInfo]
    last_year: List[DateInfo]


class WeekdayDates(BaseModel):
    """Represents specific weekday dates"""
    next_monday: DateInfo
    next_tuesday: DateInfo
    next_wednesday: DateInfo
    next_thursday: DateInfo
    next_friday: DateInfo
    next_saturday: DateInfo
    next_sunday: DateInfo
    last_monday: DateInfo
    last_tuesday: DateInfo
    last_wednesday: DateInfo
    last_thursday: DateInfo
    last_friday: DateInfo
    last_saturday: DateInfo
    last_sunday: DateInfo


class TimezoneInfo(BaseModel):
    """Represents timezone information"""
    user_timezone: str
    current_timezone: str
    is_dst: bool


class DateContext(BaseModel):
    """Complete date context response from Jarvis Command Center"""
    current: CurrentDate
    relative_dates: RelativeDates
    weekend: WeekendDates
    weeks: WeekDates
    months: MonthDates
    years: YearDates
    weekdays: WeekdayDates
    timezone: TimezoneInfo
