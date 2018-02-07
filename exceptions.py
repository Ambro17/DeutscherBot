class DeutscherBotException(Exception):
    # Abstract exception from which handled exceptions inherit.
    pass

class CouldNotGetText(DeutscherBotException):
    # Text couldn't be extracted from given object
    pass

class SearchError(DeutscherBotException):
    # Request for word search was not successfull (status !=200 )
    pass

class TranslationException(DeutscherBotException):
    # Word search result was a translation instead of a definition.
    pass