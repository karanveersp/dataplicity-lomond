# Change Log
All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/)
and this project adheres to [Semantic Versioning](http://semver.org/).


## [0.1.15] - Unreleased

### Added
- Added helpers `send_json` and `Text.json`

### Fixed
- Restored 'select' for Windows

## [0.1.14] - 2018-04-05

### Changed
- Lomond now uses Poll or KQueue depending on platform, rather than select
- Fail fast on invalid utf-8

## [0.1.13] - 2018-01-29

### Added
- Added ping_timeout
- Added SNI support

## [0.1.12] - 2017-10-17

### Fixed
- Logging tweaks
- Log writes when successful, not before

## [0.1.11] - 2017-08-09

### Added
- Added add_header method

## [0.1.10] - 2017-07-25

### Fixed
- Fixed disconnect when recv buffer is full

## [0.1.9] - 2017-06-06

### Fixed
- Change struct to use byte strings, to fix a std lib issue

## [0.1.8] - 2017-06-01

### Added
- connect() now has a close_timeout parameter.

### Changed
- WebSocket objects will now close the socket automatically if an
    exception occurs in the event loop. Negating the need to use the
    websocket as a context manager.

## [0.1.7] - 2017-05-30

### Added
- Fully tested websockets functionality
- Documentation
