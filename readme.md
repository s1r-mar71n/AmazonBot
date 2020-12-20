# 🚀🚀🚀 Amazon Bot 🚀🚀🚀

# What does the bot do: 
This bot is active on all amazontabs as soon as enabled. It only makes sense on specific sites like https://www.amazon.de/MSI-RTX-3070-Gaming-Trio/dp/B08LNPPCWJ. 
On a site like this one, it will look for the "add to cart" or the "buy now" button. If those buttons are not present, it will refresh the page. 
If they are present, the bot will try to find a price from its configuration. If a price is found and if the price is <= the price of this offer, it will press the "buy now"- button (Note: At this point, no purchase is made yet, you are one click away from making a purchase) and use an alert. 
Better notifications will be implemented in the future, an option to fully finish the buyprocess aswell. 

# How to use: 

1. clone this repository and extract it
2. head over to AmazonBot/content_script.js and configure the desired prices and items in priceObject: 

### Instructions on how to do that: 
Any number of items can be put into this json-object (priceObject), its syntax is "key" : value, "key" : value, ...
The key should be taken from the items title, for example if we visit https://www.amazon.de/MSI-RTX-3070-Gaming-Trio/dp/B08LNPPCWJ the items "key" is "MSI RTX 3070 Gaming X Trio". 
So if you want to configure your bot in order to buy this card at 800€ or lower, you would build a json-object like this: 

```
{
    "MSI RTX 3070 Gaming X Trio": 800,
    "any other item" : 1337
}
```
3. Save your configuration and head over to chrome and visit chrome://extensions
4. Choose "Load unpacked" in the left top corner and navigate to the folder "AmazonBot"

# Congrats! You did it. 

Now all you need to do is open all the tabs for those items & prices, that you configured in step 2. 

If you want to use amazon normally again, you need to disable the extension!