"""Suppress conversation logging before console, file and WebUI sinks.

This changes diagnostic output only. Message events, replies, conversation
processing, subscriptions and the plugin database are left intact.
"""
import logging
import traceback


class ConversationFilter(logging.Filter):
    def filter(self, record):
        if getattr(record, 'category', '') == 'user_chat' or record.name == 'astrbot.trace':
            return False
        path = record.pathname.replace('\\', '/')
        if path.endswith('/event_bus.py') and record.funcName == '_print_event':
            return False
        if path.endswith('/utils/trace.py') and record.funcName == 'record':
            return False
        if path.endswith('/astr_message_event.py') and record.funcName == 'clear_extra':
            return False
        if record.levelno < logging.INFO and any(part in path for part in (
            '/core/platform/', '/core/provider/', '/core/pipeline/', '/core/agent/',
        )):
            return False
        if record.exc_info and not getattr(record, '_jx3_private_exception', False):
            kind, error, tb = record.exc_info
            frames = traceback.extract_tb(tb)[-8:] if tb else []
            locations = ' > '.join(f'{frame.filename.rsplit("/", 1)[-1]}:{frame.lineno}:{frame.name}' for frame in frames)
            record.msg = '处理异常 type=%s at=%s'
            record.args = (kind.__name__ if kind else type(error).__name__, locations)
            # A fresh text-only diagnostic avoids exception messages, chained
            # exceptions and loguru's local-variable inspection leaking input.
            record.exc_info = record.exc_text = record.stack_info = None
            record._jx3_private_exception = True
        return True


class PrivacyLogging:
    def __init__(self):
        self.filter = ConversationFilter()
        self.targets = []
        self.broker_type = self.previous_publish = self.guarded_publish = None

    def install(self):
        if self.targets:
            return
        from astrbot.core.log import LogBroker
        targets = [logging.getLogger(), logging.getLogger('astrbot'), logging.getLogger('astrbot.trace')]
        targets.extend(logger for name, logger in list(logging.Logger.manager.loggerDict.items())
                       if name.startswith('astrbot.') and isinstance(logger, logging.Logger))
        for target in list(targets):
            targets.extend(target.handlers)
        for target in dict.fromkeys(targets):
            target.addFilter(self.filter)
            self.targets.append(target)

        previous = LogBroker.publish
        def publish(broker, entry):
            if entry.get('category') == 'user_chat' or entry.get('type') == 'trace':
                return
            return previous(broker, entry)
        self.broker_type, self.previous_publish, self.guarded_publish = LogBroker, previous, publish
        LogBroker.publish = publish
        # Remove only conversation/trace entries already held in the live UI
        # buffer; this does not read or erase historical log files.
        for target in self.targets:
            broker = getattr(target, 'log_broker', None)
            if broker:
                safe = [entry for entry in broker.log_cache
                        if entry.get('category') != 'user_chat' and entry.get('type') != 'trace']
                broker.log_cache.clear()
                broker.log_cache.extend(safe)

    def close(self):
        for target in self.targets:
            target.removeFilter(self.filter)
        self.targets.clear()
        if self.broker_type and self.broker_type.publish is self.guarded_publish:
            self.broker_type.publish = self.previous_publish
