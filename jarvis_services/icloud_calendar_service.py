from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import List, Dict, Any, Optional

import requests

from jarvis_log_client import JarvisLogger

from utils.date_util import parse_ical_datetime

logger = JarvisLogger(service="jarvis-node")


@dataclass
class CalendarEvent:
    """Data class for calendar events"""
    id: str
    summary: str
    start_time: datetime
    end_time: datetime
    location: Optional[str] = None
    description: Optional[str] = None
    is_all_day: bool = False
    recurrence: Optional[str] = None


class ICloudCalendarService:
    """Service for interacting with iCloud Calendar using CalDAV protocol"""
    
    def __init__(self, username: str, password: str, calendar_name: str = "Home"):
        """
        Initialize the iCloud Calendar service
        
        Args:
            username: iCloud username/Apple ID
            password: iCloud app-specific password
            calendar_name: Name of the calendar to use (default: "Home")
        """
        self.username = username
        self.password = password
        self.calendar_name = calendar_name
        
        # iCloud CalDAV endpoints to try
        self.base_urls = [
            "https://caldav.icloud.com",
            "https://p01-caldav.icloud.com", 
            "https://p02-caldav.icloud.com",
            "https://p03-caldav.icloud.com"
        ]
        self.base_url = self.base_urls[0]  # Start with the first one
        
        self.session = requests.Session()
        self._authenticated = False
        self._auth_cache_time = None
        self._auth_cache_duration = 3600  # 1 hour cache
        self.calendar_home_url = None
        
    def authenticate(self) -> bool:
        """
        Authenticate with iCloud using CalDAV protocol
        
        Returns:
            bool: True if authentication successful, False otherwise
        """
        # Check if we have a valid cached authentication
        if (self._authenticated and 
            self._auth_cache_time and 
            self.calendar_home_url and
            (datetime.now() - self._auth_cache_time).seconds < self._auth_cache_duration):
            logger.debug("Using cached authentication", cache_age_seconds=(datetime.now() - self._auth_cache_time).seconds)
            return True
        
        logger.info("Authentication cache expired or missing, performing full authentication")
        
        try:
            # Set up basic auth as per OneCal article
            self.session.auth = (self.username, self.password)
            
            # Try each base URL until one works
            for base_url in self.base_urls:
                self.base_url = base_url
                
                try:
                    # Step 1: Check server capabilities with OPTIONS
                    options_response = self.session.options(base_url)
                    
                    if options_response.status_code != 200:
                        continue
                    
                    # Step 2: Get current-user-principal from root (as per SabreDAV guide)
                    
                    principal_query = """<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
     <d:current-user-principal />
  </d:prop>
</d:propfind>"""
                    
                    principal_headers = {
                        'Content-Type': 'application/xml; charset=utf-8',
                        'Depth': '0'
                    }
                    
                    principal_response = self.session.request('PROPFIND', base_url, data=principal_query, headers=principal_headers)
                    
                    if principal_response.status_code in [200, 207]:
                        # Parse the response to find the principal URL
                        principal_url = self._extract_principal_url(principal_response.text)
                        if principal_url:
                            # Construct the full principal URL
                            if principal_url.startswith('/'):
                                full_principal_url = f"{base_url}{principal_url}"
                            else:
                                full_principal_url = f"{base_url}/{principal_url}"
                            
                            # Step 3: Get calendar-home-set from the principal (as per SabreDAV guide)
                            
                            calendar_home_query = """<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop>
     <c:calendar-home-set />
  </d:prop>
</d:propfind>"""
                            
                            calendar_home_response = self.session.request('PROPFIND', full_principal_url, data=calendar_home_query, headers=principal_headers)
                            
                            if calendar_home_response.status_code in [200, 207]:
                                # Parse the response to find the calendar home URL
                                calendar_home_url = self._extract_calendar_home_url(calendar_home_response.text)
                                if calendar_home_url:
                                    # Use the calendar home URL directly (iCloud returns absolute URLs)
                                    full_calendar_home_url = calendar_home_url
                                    
                                    # Step 4: List calendars from the calendar home (as per SabreDAV guide)
                                    
                                    list_calendars_query = """<?xml version="1.0" encoding="utf-8" ?>
<d:propfind xmlns:d="DAV:" xmlns:cs="http://calendarserver.org/ns/" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop>
     <d:resourcetype />
     <d:displayname />
     <cs:getctag />
     <c:supported-calendar-component-set />
  </d:prop>
</d:propfind>"""
                                    
                                    list_headers = {
                                        'Content-Type': 'application/xml; charset=utf-8',
                                        'Depth': '1'
                                    }
                                    
                                    list_response = self.session.request('PROPFIND', full_calendar_home_url, data=list_calendars_query, headers=list_headers)
                                    
                                    if list_response.status_code in [200, 207]:
                                        # Success! We've completed the full CalDAV discovery sequence
                                        self._authenticated = True
                                        self.calendar_home_url = calendar_home_url
                                        self._auth_cache_time = datetime.now()
                                        logger.info("Authentication successful, caching credentials for 1 hour")
                                        return True
                                    
                except Exception as e:
                    continue
            
            return False
                
        except Exception as e:
            return False
    
    def clear_auth_cache(self):
        """Clear the authentication cache to force re-authentication"""
        self._authenticated = False
        self._auth_cache_time = None
        self.calendar_home_url = None
    
    def is_authenticated(self) -> bool:
        """Check if currently authenticated (including cache validity)"""
        if (self._authenticated and 
            self._auth_cache_time and 
            self.calendar_home_url and
            (datetime.now() - self._auth_cache_time).seconds < self._auth_cache_duration):
            return True
        return False
    
    def get_cache_status(self) -> dict:
        """Get the current authentication cache status for debugging"""
        if not self._auth_cache_time:
            return {
                "cached": False,
                "reason": "No cache time set"
            }
        
        cache_age = (datetime.now() - self._auth_cache_time).seconds
        cache_valid = cache_age < self._auth_cache_duration
        
        return {
            "cached": self._authenticated and cache_valid,
            "cache_age_seconds": cache_age,
            "cache_duration_seconds": self._auth_cache_duration,
            "cache_valid": cache_valid,
            "has_calendar_home_url": bool(self.calendar_home_url),
            "authenticated": self._authenticated
        }
    
    def _extract_principal_url(self, response_text: str) -> str:
        """Extract the principal URL from the current-user-principal response"""
        try:
            lines = response_text.split('\n')
            for line in lines:
                # Look for current-user-principal with or without namespace prefix
                if ('<current-user-principal' in line and '</current-user-principal>' in line) or \
                   ('<d:current-user-principal>' in line and '</d:current-user-principal>' in line):
                    
                    # Extract the href value - look for href with or without namespace prefix
                    if '<href' in line and '</href>' in line:
                        # Find the start of the href value
                        href_start = line.find('<href')
                        if href_start != -1:
                            # Find the > after href
                            href_start = line.find('>', href_start) + 1
                            href_end = line.find('</href>')
                            if href_start < href_end:
                                href = line[href_start:href_end]
                                return href
            return None
        except Exception as e:
            return None
    
    def _extract_calendar_home_url(self, response_text: str) -> str:
        """Extract the calendar home URL from the calendar-home-set response"""
        try:
            lines = response_text.split('\n')
            for line in lines:
                # Look for calendar-home-set with or without namespace prefix
                if ('<calendar-home-set' in line and '</calendar-home-set>' in line) or \
                   ('<c:calendar-home-set>' in line and '</c:calendar-home-set>' in line):
                    
                    # Extract the href value - look for href with or without namespace prefix
                    if '<href' in line and '</href>' in line:
                        # Find the start of href value
                        href_start = line.find('<href')
                        if href_start != -1:
                            # Find the > after href
                            href_start = line.find('>', href_start) + 1
                            href_end = line.find('</href>')
                            if href_start < href_end:
                                href = line[href_start:href_end]
                                return href
            return None
        except Exception as e:
            return None
    
    def _find_calendar_url(self, response_text: str, calendar_name: str) -> str:
        """Find the URL of a specific calendar by name"""
        try:
            lines = response_text.split('\n')
            current_href = None
            
            for i, line in enumerate(lines):
                # Look for href lines
                if '<href>' in line and '</href>' in line:
                    href_start = line.find('<href>') + 6
                    href_end = line.find('</href>')
                    if href_start < href_end:
                        current_href = line[href_start:href_end]
                
                # Look for displayname lines (handle both formats)
                elif '<displayname' in line and '</displayname>' in line:
                    # Handle both <displayname>text</displayname> and <displayname xmlns="DAV:">text</displayname>
                    if 'xmlns="DAV:"' in line:
                        # Format: <displayname xmlns="DAV:">text</displayname>
                        name_start = line.find('>', line.find('xmlns="DAV:"')) + 1
                    else:
                        # Format: <displayname>text</displayname>
                        name_start = line.find('<displayname>') + 13
                    
                    name_end = line.find('</displayname>')
                    if name_start < name_end:
                        display_name = line[name_start:name_end]
                        
                        # Check if this is the calendar we want (case-insensitive)
                        if display_name.lower() == calendar_name.lower():
                            return current_href
            
            return None
            
        except Exception as e:
            return None
    
    def read_events(self, date: Optional[datetime] = None, look_ahead_days: int = 1) -> List[CalendarEvent]:
        """
        Read calendar events for a specific date or date range
        
        Args:
            date: Start date (default: today)
            look_ahead_days: Number of days to look ahead (default: 1)
            
        Returns:
            List of CalendarEvent objects
        """
        if not self._authenticated and not self.authenticate():
            return []
        
        if date is None:
            date = datetime.now()
        
        # Calculate date range
        end_date = date + timedelta(days=look_ahead_days)
        
        try:
            # Build CalDAV query for events in date range
            # According to OneCal, we need to discover the actual calendar URL first
            
            # Use the discovered calendar home URL directly (it already contains /calendars/)
            if not hasattr(self, 'calendar_home_url'):
                logger.debug("No calendar_home_url found, returning empty list")
                return []
                
            calendars_url = self.calendar_home_url
            
            # Use PROPFIND to discover calendars as per OneCal article
            propfind_query = """<?xml version="1.0" encoding="utf-8" ?>
<D:propfind xmlns:D="DAV:">
    <D:prop>
        <D:resourcetype/>
        <D:displayname/>
        <D:getctag/>
    </D:prop>
</D:propfind>"""
            
            propfind_headers = {
                'Content-Type': 'application/xml; charset=utf-8',
                'Depth': '1'
            }
            
            try:
                logger.debug("Discovering calendars", url=calendars_url)
                calendars_response = self.session.request('PROPFIND', calendars_url, data=propfind_query, headers=propfind_headers)
                logger.debug("Calendar discovery response", status_code=calendars_response.status_code)
                
                if calendars_response.status_code in [200, 207]:
                    # Parse the calendar list to find the specific calendar we want
                    calendar_url = self._find_calendar_url(calendars_response.text, self.calendar_name)
                    if not calendar_url:
                        logger.debug("Calendar not found in response, returning empty list", calendar_name=self.calendar_name)
                        return []
                    
                    # Construct the full calendar URL
                    if calendar_url.startswith('/'):
                        full_calendar_url = f"{self.base_url}{calendar_url}"
                    else:
                        full_calendar_url = calendar_url
                    
                    # Now try to query events using the calendar-query REPORT
                    # This follows the OneCal article's approach
                    
                    # Debug: Show the exact dates being used
                    logger.debug("Querying calendar for date range", start=date.strftime('%Y-%m-%d %H:%M:%S'), end=end_date.strftime('%Y-%m-%d %H:%M:%S'))
                    
                    calendar_query = f"""<?xml version="1.0" encoding="utf-8" ?>
<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
    <D:prop>
        <D:getetag/>
        <C:calendar-data/>
    </D:prop>
    <C:filter>
        <C:comp-filter name="VCALENDAR">
            <C:comp-filter name="VEVENT">
                <C:time-range start="{date.strftime('%Y%m%dT%H%M%SZ')}" end="{end_date.strftime('%Y%m%dT%H%M%SZ')}"/>
            </C:comp-filter>
        </C:comp-filter>
    </C:filter>
</C:calendar-query>"""
                    
            
                    
                    headers = {
                        'Content-Type': 'application/xml; charset=utf-8',
                        'Depth': '1'
                    }
                    
                    response = self.session.request('REPORT', full_calendar_url, data=calendar_query, headers=headers)
                    
                    if response.status_code in [200, 207]:  # CalDAV returns 207 Multi-Status for successful queries
                        logger.debug("Calendar query successful", status_code=response.status_code)
                        return self._parse_calendar_response(response.text, date, end_date)
                    else:
                        logger.warning("Calendar query failed", status_code=response.status_code)
                        return []
                        
                else:
                    return []
                    
            except Exception as e:
                return []
                
        except Exception as e:
            return []
    
    def _parse_calendar_response(self, response_text: str, start_date: datetime, end_date: datetime) -> List[CalendarEvent]:
        """
        Parse the CalDAV response into CalendarEvent objects
        
        Args:
            response_text: Raw CalDAV response
            start_date: Start date for filtering events
            end_date: End date for filtering events
            
        Returns:
            List of parsed CalendarEvent objects
        """
        events = []
        
        try:
            # Parse the CalDAV XML response to extract calendar-data
            lines = response_text.split('\n')
            in_calendar_data = False
            calendar_data = ""
            
            for line in lines:
                if '<calendar-data' in line:
                    in_calendar_data = True
                    # Extract the content after the opening tag
                    start_tag_end = line.find('>') + 1
                    if start_tag_end > 0:
                        calendar_data += line[start_tag_end:]
                elif '</calendar-data>' in line:
                    in_calendar_data = False
                    # Extract the content before the closing tag
                    end_tag_start = line.find('</calendar-data>')
                    if end_tag_start > 0:
                        calendar_data += line[:end_tag_start]
                    
                    # Parse this event's iCal content immediately
                    if calendar_data.strip():
                        event_events = self._parse_ical_content(calendar_data, start_date, end_date)
                        events.extend(event_events)
                    
                    # Reset for next event
                    calendar_data = ""
                elif in_calendar_data:
                    calendar_data += line + '\n'
            
            # Parse any remaining calendar data
            if calendar_data.strip():
                event_events = self._parse_ical_content(calendar_data, start_date, end_date)
                events.extend(event_events)
            
            return events
            
        except Exception as e:
            return []
    
    def add_event(self, event_data: Dict[str, Any]) -> bool:
        """
        Add a new calendar event
        
        Args:
            event_data: Dictionary containing event details
            
        Returns:
            bool: True if successful, False otherwise
        """
        if not self._authenticated and not self.authenticate():
            return False
        
        try:
            # Build iCal event data
            event_id = f"event_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            
            # Create iCal format event
            ical_event = self._create_ical_event(event_id, event_data)
            
            # POST to calendar
            calendar_url = f"{self.base_url}/{self.username}/calendars/{self.calendar_name}/{event_id}.ics"
            
            headers = {
                'Content-Type': 'text/calendar; charset=utf-8'
            }
            
            response = self.session.put(calendar_url, data=ical_event, headers=headers)
            
            return response.status_code in [200, 201]
            
        except Exception as e:
            return False
    
    def _create_ical_event(self, event_id: str, event_data: Dict[str, Any]) -> str:
        """
        Create an iCal formatted event string
        
        Args:
            event_id: Unique identifier for the event
            event_data: Event details
            
        Returns:
            iCal formatted string
        """
        # Basic iCal format
        ical = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Jarvis//Calendar Service//EN
BEGIN:VEVENT
UID:{event_id}
DTSTAMP:{datetime.now().strftime('%Y%m%dT%H%M%SZ')}
DTSTART:{event_data.get('start_time', datetime.now()).strftime('%Y%m%dT%H%M%SZ')}
DTEND:{event_data.get('end_time', datetime.now() + timedelta(hours=1)).strftime('%Y%m%dT%H%M%SZ')}
SUMMARY:{event_data.get('summary', 'New Event')}
"""
        
        if event_data.get('location'):
            ical += f"LOCATION:{event_data['location']}\n"
        
        if event_data.get('description'):
            ical += f"DESCRIPTION:{event_data['description']}\n"
        
        ical += """END:VEVENT
END:VCALENDAR"""
        
        return ical
    
    def update_event(self, event_id: str, event_data: Dict[str, Any]) -> bool:
        """
        Update an existing calendar event
        
        Args:
            event_id: ID of the event to update
            event_data: Updated event details
            
        Returns:
            bool: True if successful, False otherwise
        """
        if not self._authenticated and not self.authenticate():
            return False
        
        try:
            # For updates, we typically need to get the current event first
            # then modify and PUT it back
            # This is a simplified implementation
            
            # Create updated iCal event
            ical_event = self._create_ical_event(event_id, event_data)
            
            # PUT to update
            calendar_url = f"{self.base_url}/{self.username}/calendars/{self.calendar_name}/{event_id}.ics"
            
            headers = {
                'Content-Type': 'text/calendar; charset=utf-8'
            }
            
            response = self.session.put(calendar_url, data=ical_event, headers=headers)
            
            return response.status_code == 200
            
        except Exception as e:
            return False
    
    def delete_event(self, event_id: str) -> bool:
        """
        Delete a calendar event
        
        Args:
            event_id: ID of the event to delete
            
        Returns:
            bool: True if successful, False otherwise
        """
        if not self._authenticated and not self.authenticate():
            return False
        
        try:
            calendar_url = f"{self.base_url}/{self.username}/calendars/{self.calendar_name}/{event_id}.ics"
            
            response = self.session.delete(calendar_url)
            
            return response.status_code == 200
            
        except Exception as e:
            return False
    
    def get_calendar_list(self) -> List[str]:
        """
        Get list of available calendars
        
        Returns:
            List of calendar names
        """
        if not self._authenticated and not self.authenticate():
            return []
        
        try:
            # Query for available calendars
            response = self.session.propfind(f"{self.base_url}/{self.username}/calendars/")
            
            if response.status_code == 200:
                # Parse response to extract calendar names
                # This would need proper XML parsing in a real implementation
                return [self.calendar_name]  # Placeholder
            else:
                return []
                
        except Exception as e:
            return []
    

    
    def _parse_ical_content(self, ical_content: str, start_date: datetime, end_date: datetime) -> List[CalendarEvent]:
        """
        Parse iCal content and filter events by date range
        
        Args:
            ical_content: Raw iCal content
            start_date: Start date for filtering
            end_date: End date for filtering
            
        Returns:
            List of CalendarEvent objects
        """
        try:
            # This is a simplified parser - in practice, you'd want to use the 'icalendar' library
            # For now, we'll return a basic structure
            events = []
            
            # Basic iCal parsing (handle semicolon-separated parameters)
            lines = ical_content.split('\n')
            current_event = {}
            
            for line in lines:
                line = line.strip()
                if line.startswith('BEGIN:VEVENT'):
                    current_event = {}
                elif line.startswith('END:VEVENT'):
                    if current_event:
                        # Create CalendarEvent object
                        try:
                            # Parse datetime strings to datetime objects with timezone info
                            start_time = self._parse_ical_datetime(
                                current_event.get('dtstart'), 
                                current_event.get('dtstart_full')
                            )
                            end_time = self._parse_ical_datetime(
                                current_event.get('dtend'), 
                                current_event.get('dtend_full')
                            )
                            
                            # Handle events with missing start time (like notes/reminders)
                            if not start_time and end_time:
                                # If no start time but has end time, use end time as start time
                                # This is common for reminder notes
                                start_time = end_time

                            
                            # Only include events that have both start and end times
                            if start_time and end_time:
                                # Check if event overlaps with requested range
                                # Event starts before end_date AND ends after start_date
                                if start_time < end_date and end_time > start_date:
                                    event = CalendarEvent(
                                        id=current_event.get('uid', f"event_{len(events)}"),
                                        summary=current_event.get('summary', 'No Title'),
                                        start_time=start_time,
                                        end_time=end_time,
                                        location=current_event.get('location'),
                                        description=current_event.get('description'),
                                        is_all_day=current_event.get('dtstart', '').endswith('VALUE=DATE:'),
                                        recurrence=current_event.get('rrule')
                                    )
                                    events.append(event)

                        except Exception as e:
                            continue
                elif ':' in line:
                    # Handle iCal format: KEY;PARAM1=VAL1;PARAM2=VAL2:VALUE
                    parts = line.split(':', 1)
                    if len(parts) == 2:
                        key_part = parts[0]
                        value = parts[1]
                        
                        # Extract the base key (before any semicolons)
                        base_key = key_part.split(';')[0].lower()
                        current_event[base_key] = value
                        
                        # Store the full line for timezone extraction
                        current_event[f"{base_key}_full"] = line
                        

            
            return events
            
        except Exception as e:
            return []
    
    def _parse_ical_datetime(self, datetime_str: str, timezone_info: str = None) -> Optional[datetime]:
        """Use the utility function for parsing iCal datetime strings"""
        return parse_ical_datetime(datetime_str, timezone_info)
