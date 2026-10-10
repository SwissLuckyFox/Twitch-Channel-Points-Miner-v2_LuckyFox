class StreamerDoesNotExistException(Exception):
    pass


class StreamerIsOfflineException(Exception):
    pass


class WrongCookiesException(Exception):
    pass


class BadCredentialsException(Exception):
    pass


class UnexpectedTwitchResponseException(Exception):
    pass
