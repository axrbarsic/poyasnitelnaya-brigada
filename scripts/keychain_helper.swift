import Foundation
import Security

enum HelperError: Error {
    case invalidArguments
    case emptySecret
    case invalidSecretData
    case keychain(OSStatus)
}

func query(service: String, account: String) -> [String: Any] {
    return [
        kSecClass as String: kSecClassGenericPassword,
        kSecAttrService as String: service,
        kSecAttrAccount as String: account,
    ]
}

func setSecret(service: String, account: String) throws {
    let input = FileHandle.standardInput.readDataToEndOfFile()
    guard var secret = String(data: input, encoding: .utf8) else {
        throw HelperError.invalidSecretData
    }
    secret = secret.trimmingCharacters(in: .newlines)
    guard !secret.isEmpty else {
        throw HelperError.emptySecret
    }
    guard let secretData = secret.data(using: .utf8) else {
        throw HelperError.invalidSecretData
    }

    let itemQuery = query(service: service, account: account)
    let update: [String: Any] = [kSecValueData as String: secretData]
    var status = SecItemUpdate(itemQuery as CFDictionary, update as CFDictionary)
    if status == errSecItemNotFound {
        var addition = itemQuery
        addition[kSecValueData as String] = secretData
        status = SecItemAdd(addition as CFDictionary, nil)
    }
    secret = ""
    guard status == errSecSuccess else {
        throw HelperError.keychain(status)
    }
}

func getSecret(service: String, account: String) throws {
    var itemQuery = query(service: service, account: account)
    itemQuery[kSecReturnData as String] = true
    itemQuery[kSecMatchLimit as String] = kSecMatchLimitOne
    var result: CFTypeRef?
    let status = SecItemCopyMatching(itemQuery as CFDictionary, &result)
    guard status == errSecSuccess else {
        throw HelperError.keychain(status)
    }
    guard let data = result as? Data, !data.isEmpty else {
        throw HelperError.emptySecret
    }
    FileHandle.standardOutput.write(data)
}

func secretExists(service: String, account: String) throws {
    var itemQuery = query(service: service, account: account)
    itemQuery[kSecReturnData as String] = false
    itemQuery[kSecMatchLimit as String] = kSecMatchLimitOne
    let status = SecItemCopyMatching(itemQuery as CFDictionary, nil)
    guard status == errSecSuccess else {
        throw HelperError.keychain(status)
    }
}

do {
    guard CommandLine.arguments.count == 4 else {
        throw HelperError.invalidArguments
    }
    let command = CommandLine.arguments[1]
    let service = CommandLine.arguments[2]
    let account = CommandLine.arguments[3]
    switch command {
    case "set":
        try setSecret(service: service, account: account)
    case "get":
        try getSecret(service: service, account: account)
    case "exists":
        try secretExists(service: service, account: account)
    default:
        throw HelperError.invalidArguments
    }
} catch {
    exit(1)
}
