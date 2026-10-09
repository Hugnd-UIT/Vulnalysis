#import <Foundation/Foundation.h>
@implementation F1
+ (void)helper {}
- (void)intra { [F1 helper]; }
@end
